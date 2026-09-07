"""
Tests for Audio Sinks - Manual control implementation.

These tests verify the complex audio processing pipelines, VAD integration,
wake word detection, threading safety, and proper resource cleanup for
ManualControlSink.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import discord
from discord.ext import voice_recv

from src.audio.sinks import ManualControlSink, VADAnalyzer, CleanupMetrics
from src.bot.state import BotState, BotStateEnum, RecordingMethod
from src.config.config import Config
from src.exceptions import SessionConsistencyError


class TestCleanupMetrics:
    """Test the cleanup metrics tracking functionality."""

    @pytest.fixture
    def metrics(self):
        return CleanupMetrics()

    def test_initialization(self, metrics):
        """Test CleanupMetrics initializes with correct default values."""
        assert metrics.comprehensive_cleanups == 0
        assert metrics.race_conditions_prevented == 0
        assert metrics.cleanup_errors == 0
        assert metrics.zero_byte_recordings_prevented == 0
        assert metrics.successful_interactions == 0
        assert isinstance(metrics.last_successful_cleanup, float)

    def test_record_comprehensive_cleanup(self, metrics):
        """Test recording comprehensive cleanup operations."""
        stats = {"errors": 2, "buffers_cleared": 5}

        metrics.record_comprehensive_cleanup(stats)

        assert metrics.comprehensive_cleanups == 1
        assert metrics.cleanup_errors == 2

    def test_record_race_condition_prevention(self, metrics):
        """Test recording race condition prevention."""
        metrics.record_race_condition_prevention(123, 456)

        assert metrics.race_conditions_prevented == 1

    def test_record_race_condition_prevention_same_id(self, metrics):
        """Test recording race condition when IDs match."""
        metrics.record_race_condition_prevention(123, 123)

        # Should not increment if IDs match
        assert metrics.race_conditions_prevented == 0

    def test_record_session_id_mismatch(self, metrics):
        """Test recording session ID mismatch."""
        metrics.record_session_id_mismatch(100, 200)

        assert metrics.race_conditions_prevented == 1

    def test_record_successful_interaction(self, metrics):
        """Test recording successful interactions."""
        metrics.record_successful_interaction(1024)

        assert metrics.successful_interactions == 1

    def test_export_health_report(self, metrics):
        """Test exporting health report."""
        metrics.comprehensive_cleanups = 5
        metrics.race_conditions_prevented = 3
        metrics.cleanup_errors = 1
        metrics.successful_interactions = 10

        report = metrics.export_health_report()

        assert report["total_cleanups"] == 5
        assert report["race_conditions_prevented"] == 3
        assert report["cleanup_error_rate"] == 0.2  # 1/5
        assert report["successful_interactions"] == 10


class TestVADAnalyzer:
    """Test Voice Activity Detection analyzer."""

    @pytest.fixture
    def mock_callback(self):
        return AsyncMock()

    @pytest.fixture
    def mock_loop(self):
        return AsyncMock(spec=asyncio.AbstractEventLoop)

    @pytest.fixture
    def vad_analyzer(self, mock_callback, mock_loop):
        with patch("webrtcvad.Vad"):
            return VADAnalyzer(
                on_speech_end=mock_callback,
                sample_rate=16000,
                frame_duration_ms=20,
                min_speech_duration_ms=100,
                silence_timeout_ms=1000,
                grace_period_ms=200,
                loop=mock_loop,
            )

    def test_initialization(self, vad_analyzer, mock_callback, mock_loop):
        """Test VADAnalyzer initializes correctly."""
        assert vad_analyzer._on_speech_end == mock_callback
        assert vad_analyzer._loop == mock_loop
        assert vad_analyzer._sample_rate == 16000
        assert vad_analyzer._frame_duration_ms == 20
        assert not vad_analyzer._is_speech
        assert not vad_analyzer._triggered

    def test_process_speech_frame(self, vad_analyzer):
        """Test processing a speech frame."""
        # Create proper frame data
        frame_data = b"\x00" * vad_analyzer._frame_bytes

        # Mock webrtcvad to return True for speech
        vad_analyzer._vad.is_speech.return_value = True

        vad_analyzer.process(frame_data)

        assert vad_analyzer._speech_frame_count > 0

    def test_process_non_speech_frame(self, vad_analyzer):
        """Test processing a non-speech frame."""
        # Create proper frame data
        frame_data = b"\x00" * vad_analyzer._frame_bytes

        # Mock webrtcvad to return False for speech
        vad_analyzer._vad.is_speech.return_value = False

        vad_analyzer.process(frame_data)

        assert vad_analyzer._frames_processed > 0

    def test_initial_silence_eventually_ends_empty_turn(self, vad_analyzer):
        frame_data = b"\x00" * vad_analyzer._frame_bytes
        vad_analyzer._vad.is_speech.return_value = False

        for _ in range(vad_analyzer._silence_frames_timeout + 1):
            vad_analyzer.process(frame_data)

        assert vad_analyzer._triggered is True
        vad_analyzer._loop.call_soon_threadsafe.assert_called_once()

    def test_process_invalid_frame_size(self, vad_analyzer):
        """Test processing frame with invalid size."""
        # Invalid frame size
        frame_data = b"too_short"

        vad_analyzer.process(frame_data)

        # Should not crash, just log debug message

    def test_reset(self, vad_analyzer):
        """Test VAD analyzer reset functionality."""
        vad_analyzer._speech_frame_count = 5
        vad_analyzer._silence_frame_count = 3
        vad_analyzer._is_speech = True
        vad_analyzer._triggered = True

        vad_analyzer.reset()

        assert vad_analyzer._speech_frame_count == 0
        assert vad_analyzer._silence_frame_count == 0
        assert not vad_analyzer._is_speech
        assert not vad_analyzer._triggered


# Note: AudioSinkBase tests removed as AudioSink is an abstract base class
# that cannot be instantiated directly. Tests for ManualControlSink provide coverage
# for the implemented audio sink functionality.

# Note: RealtimeMixingSink tests not included as it's a skeleton implementation
# for future features and is not currently used in the project.


class TestManualControlSink:
    """Test the ManualControlSink implementation."""

    @pytest.fixture
    def mock_bot_state(self):
        bot_state = MagicMock(spec=BotState)
        bot_state.current_state = BotStateEnum.STANDBY
        bot_state.current_session_id = 100
        return bot_state

    @pytest.fixture
    def mock_callbacks(self):
        return {
            "on_wake_word_detected": AsyncMock(),
            "on_vad_speech_end": AsyncMock(),
        }

    @pytest.fixture
    def mock_action_lock(self):
        return asyncio.Lock()

    @pytest.fixture
    def manual_control_sink(self, mock_bot_state, mock_callbacks, mock_action_lock):
        """Create ManualControlSink with mocked dependencies."""
        mock_loop = AsyncMock()
        mock_task = MagicMock()
        with (
            patch.multiple(
                "src.audio.sinks",
                UnifiedAudioProcessor=MagicMock(),
                Model=MagicMock(),
            ),
            patch("src.audio.sinks.Config.WAKE_WORD_ENGINE", "openwakeword"),
            patch("asyncio.get_running_loop", return_value=mock_loop),
            patch("asyncio.create_task", return_value=mock_task),
        ):
            sink = ManualControlSink(
                bot_state=mock_bot_state,
                initial_consented_users={123, 456},
                on_wake_word_detected=mock_callbacks["on_wake_word_detected"],
                on_vad_speech_end=mock_callbacks["on_vad_speech_end"],
                action_lock=mock_action_lock,
            )
            # Keep the patched detector/config globals active for the complete
            # lifetime of each test.  Returning here used to work only while
            # openWakeWord was the process-wide default; after adding the
            # sherpa-onnx engine, methods invoked by the test could otherwise
            # instantiate the real Chinese model.
            yield sink

    def test_initialization(self, manual_control_sink, mock_bot_state, mock_callbacks):
        """Test ManualControlSink initialization."""
        assert manual_control_sink._bot_state == mock_bot_state
        assert (
            manual_control_sink._on_wake_word_detected
            == mock_callbacks["on_wake_word_detected"]
        )
        assert (
            manual_control_sink._on_vad_speech_end
            == mock_callbacks["on_vad_speech_end"]
        )
        assert 123 in manual_control_sink._detectors
        assert 456 in manual_control_sink._detectors
        assert manual_control_sink._session_id_at_creation == 100
        assert not manual_control_sink._is_vad_enabled

    def test_add_user_creates_detector(self, manual_control_sink):
        """Test adding a user creates wake word detector."""
        with patch("src.audio.sinks.Model") as mock_model:
            mock_detector = MagicMock()
            mock_model.return_value = mock_detector

            manual_control_sink.add_user(789)

            # User gets wake word detector created
            assert 789 in manual_control_sink._detectors
            assert 789 in manual_control_sink._user_audio_buffers
            assert 789 in manual_control_sink._ww_resampled_buffers
            assert 789 in manual_control_sink._ww_buffer_locks

    def test_add_user_already_exists(self, manual_control_sink):
        """Test adding user that already exists."""
        # Add user first time
        with patch("src.audio.sinks.Model"):
            manual_control_sink.add_user(123)
            detector_count = len(manual_control_sink._detectors)

        # Add same user again
        manual_control_sink.add_user(123)

        # Should not create additional detector
        assert len(manual_control_sink._detectors) == detector_count

    def test_add_user_model_creation_failure(self, manual_control_sink):
        """Test handling wake word model creation failure."""
        with patch(
            "src.audio.sinks.Model", side_effect=Exception("Model creation failed")
        ):
            # Should not raise exception
            manual_control_sink.add_user(789)

            # Note: User doesn't get detector on model creation failure
            assert 789 not in manual_control_sink._detectors  # No detector created

    def test_remove_user_cleans_up_resources(self, manual_control_sink):
        """Test removing user cleans up all associated resources."""
        # First add a user
        with patch("src.audio.sinks.Model"):
            manual_control_sink.add_user(789)

        # Then remove the user
        manual_control_sink.remove_user(789)

        # Verify user was removed from all tracking
        assert 789 not in manual_control_sink._detectors
        assert 789 not in manual_control_sink._user_audio_buffers
        assert 789 not in manual_control_sink._ww_resampled_buffers
        assert 789 not in manual_control_sink._ww_buffer_locks

    def test_enable_vad_initializes_analyzer(self, manual_control_sink):
        """Test enabling VAD initializes the analyzer."""
        with patch("src.audio.sinks.VADAnalyzer") as mock_vad_analyzer:
            mock_analyzer = MagicMock()
            mock_vad_analyzer.return_value = mock_analyzer

            manual_control_sink.enable_vad(True)

            assert manual_control_sink._is_vad_enabled is True
            assert manual_control_sink._vad_analyzer is not None
            # VAD analyzer should have been created
            mock_vad_analyzer.assert_called_once()

    def test_enable_vad_accepts_realtime_safety_timeout(self, manual_control_sink):
        with patch("src.audio.sinks.VADAnalyzer") as mock_vad_analyzer:
            manual_control_sink.enable_vad(True, silence_timeout_ms=10000)

            assert mock_vad_analyzer.call_args.kwargs["silence_timeout_ms"] == 10000

    def test_enable_vad_false_resets_state(self, manual_control_sink):
        """Test disabling VAD resets all VAD-related state."""
        # First enable VAD
        with patch("src.audio.sinks.VADAnalyzer"):
            manual_control_sink.enable_vad(True)

            # Add some audio to buffers to test clearing
            manual_control_sink._vad_raw_buffer.extend(b"test_data")
            manual_control_sink._vad_resampled_buffer.extend(b"resampled_data")

        # Now disable VAD
        manual_control_sink.enable_vad(False)

        # Verify state was reset
        assert manual_control_sink._is_vad_enabled is False
        assert manual_control_sink._vad_analyzer is None
        assert len(manual_control_sink._vad_raw_buffer) == 0
        assert len(manual_control_sink._vad_resampled_buffer) == 0

    def test_update_session_id(self, manual_control_sink):
        """Test updating session ID."""
        manual_control_sink._bot_state.current_session_id = 200

        manual_control_sink.update_session_id()

        assert manual_control_sink._active_session_id == 200

    def test_stop_and_get_audio_returns_authority_buffer(self, manual_control_sink):
        """Test stop_and_get_audio returns and clears authority buffer."""
        test_audio = b"test_audio_data"
        manual_control_sink._authority_buffer.extend(test_audio)

        result = manual_control_sink.stop_and_get_audio()

        assert result == test_audio
        assert len(manual_control_sink._authority_buffer) == 0

    def test_stop_and_get_audio_raises_on_session_mismatch(self, manual_control_sink):
        """Test stop_and_get_audio raises SessionConsistencyError on session ID mismatch."""
        test_audio = b"test_audio_data"
        manual_control_sink._authority_buffer.extend(test_audio)

        # Set up session ID mismatch: sink has ID 100, bot state has ID 200
        manual_control_sink._active_session_id = 100
        manual_control_sink._bot_state.current_session_id = 200

        # Should raise SessionConsistencyError
        with pytest.raises(SessionConsistencyError) as exc_info:
            manual_control_sink.stop_and_get_audio()

        # Verify error message contains session IDs
        assert "100" in str(exc_info.value)
        assert "200" in str(exc_info.value)

        # Verify audio buffer was NOT cleared (exception raised before cleanup)
        assert len(manual_control_sink._authority_buffer) == len(test_audio)

    def test_write_with_disallowed_user(self, manual_control_sink):
        """Test write method ignores disallowed users."""
        user = MagicMock(spec=discord.User)
        user.id = 999  # Not in allowed users
        voice_data = MagicMock(spec=voice_recv.VoiceData)
        voice_data.pcm = b"test_audio"

        # Should not raise exception and should not process
        manual_control_sink.write(user, voice_data)

    def test_write_with_allowed_user(self, manual_control_sink):
        """Test write method processes allowed users."""
        user = MagicMock(spec=discord.User)
        user.id = 123
        voice_data = MagicMock(spec=voice_recv.VoiceData)
        voice_data.pcm = b"test_audio_data"

        # Mock session state
        manual_control_sink._bot_state.current_state = BotStateEnum.RECORDING
        manual_control_sink._bot_state.recording_method = RecordingMethod.PushToTalk
        manual_control_sink._bot_state.is_authorized.return_value = True
        manual_control_sink._active_session_id = 100

        # Should not raise exception
        manual_control_sink.write(user, voice_data)

    def test_recording_keeps_new_participant_wake_detector_active(
        self, manual_control_sink
    ):
        user = MagicMock(spec=discord.User)
        user.id = 456
        voice_data = MagicMock(spec=voice_recv.VoiceData)
        voice_data.pcm = b"test_audio_data"
        manual_control_sink._bot_state.current_state = BotStateEnum.RECORDING
        manual_control_sink._bot_state.is_authorized.return_value = False
        manual_control_sink._bot_state.is_active_participant.return_value = False
        manual_control_sink._process_standby_audio = MagicMock()

        manual_control_sink.write(user, voice_data)

        manual_control_sink._process_standby_audio.assert_called_once_with(
            user, voice_data
        )

    def test_write_session_id_mismatch_prevention(self, manual_control_sink):
        """Test write method auto-updates session ID to prevent contamination."""
        user = MagicMock(spec=discord.User)
        user.id = 123
        voice_data = MagicMock(spec=voice_recv.VoiceData)
        voice_data.pcm = b"test_audio_data"

        # Set up session ID mismatch
        manual_control_sink._active_session_id = 100
        manual_control_sink._bot_state.current_session_id = 200

        manual_control_sink.write(user, voice_data)

        # Should have auto-updated the session ID to prevent contamination
        assert manual_control_sink._active_session_id == 200

    def test_active_participant_requires_sustained_speech(self, manual_control_sink):
        user = MagicMock(spec=discord.User)
        user.id = 123
        voice_data = MagicMock(spec=voice_recv.VoiceData)
        voice_data.pcm = b"\x01\x00" * (Config.VAD_PROCESSING_CHUNK // 2)
        manual_control_sink._on_active_speech_detected = AsyncMock()
        manual_control_sink._bot_state.current_state = BotStateEnum.STANDBY
        manual_control_sink._bot_state.is_active_participant.return_value = True
        manual_control_sink._audio_processor.convert_sync.return_value = (
            b"\x01\x00" * 640
        )
        manual_control_sink._active_speech_vads[user.id] = MagicMock()
        manual_control_sink._active_speech_vads[user.id].is_speech.return_value = True

        manual_control_sink.write(user, voice_data)

        manual_control_sink._loop.call_soon_threadsafe.assert_not_called()

        for _ in range(5):
            manual_control_sink.write(user, voice_data)

        manual_control_sink._loop.call_soon_threadsafe.assert_called_once()
        assert user.id in manual_control_sink._active_speech_pending

    def test_active_participant_background_noise_does_not_barge_in(
        self, manual_control_sink
    ):
        user = MagicMock(spec=discord.User)
        user.id = 123
        voice_data = MagicMock(spec=voice_recv.VoiceData)
        voice_data.pcm = b"\x00" * Config.VAD_PROCESSING_CHUNK
        manual_control_sink._on_active_speech_detected = AsyncMock()
        manual_control_sink._bot_state.current_state = BotStateEnum.STANDBY
        manual_control_sink._bot_state.is_active_participant.return_value = True
        manual_control_sink._audio_processor.convert_sync.return_value = b"\x00" * 1280
        manual_control_sink._active_speech_vads[user.id] = MagicMock()
        manual_control_sink._active_speech_vads[user.id].is_speech.return_value = False

        for _ in range(20):
            manual_control_sink.write(user, voice_data)

        manual_control_sink._loop.call_soon_threadsafe.assert_not_called()
        assert user.id not in manual_control_sink._active_speech_pending

    @pytest.mark.parametrize("initial_state", [BotStateEnum.STANDBY, BotStateEnum.RECORDING])
    def test_continuous_speech_is_not_a_new_interruption(
        self, manual_control_sink, initial_state
    ):
        """The same utterance must not re-trigger when the bot begins playback."""
        sink = manual_control_sink
        user = MagicMock(id=123)
        data = MagicMock(pcm=b"\x01\x00" * (Config.VAD_PROCESSING_CHUNK // 2))
        sink._on_active_speech_detected = AsyncMock()
        sink._bot_state.current_state = initial_state
        sink._bot_state.is_active_participant.return_value = True
        sink._bot_state.is_authorized.return_value = False
        sink._audio_processor.convert_sync.return_value = b"\x01\x00" * 640
        sink._active_speech_vads[user.id] = MagicMock()
        sink._active_speech_vads[user.id].is_speech.return_value = True
        with patch("src.audio.sinks.time.monotonic", return_value=10.0) as clock:
            for index in range(120):
                clock.return_value = 10.0 + index * 0.02
                if index == 30:
                    sink._bot_state.current_state = BotStateEnum.STANDBY
                # The notification finishes, but this speaker never fell silent.
                sink._active_speech_pending.discard(user.id)
                sink.write(user, data)
        expected = 1 if initial_state == BotStateEnum.STANDBY else 0
        assert sink._loop.call_soon_threadsafe.call_count == expected

    @pytest.mark.parametrize("rtp_gap", [True, False])
    def test_new_utterance_rearms_one_user_without_resetting_teammate(
        self, manual_control_sink, rtp_gap
    ):
        """Both explicit silence and Discord packet gaps permit the next turn."""
        sink = manual_control_sink
        users = [MagicMock(id=123), MagicMock(id=456)]
        data = MagicMock(pcm=b"\x01\x00" * (Config.VAD_PROCESSING_CHUNK // 2))
        sink._on_active_speech_detected = AsyncMock()
        sink._bot_state.current_state = BotStateEnum.STANDBY
        sink._bot_state.is_active_participant.return_value = True
        sink._audio_processor.convert_sync.return_value = b"\x01\x00" * 640
        for user in users:
            sink._active_speech_vads[user.id] = MagicMock()
            sink._active_speech_vads[user.id].is_speech.return_value = True
        with patch("src.audio.sinks.time.monotonic", return_value=10.0) as clock:
            for index in range(10):
                clock.return_value = 10.0 + index * 0.04
                for user in users:
                    sink.write(user, data)
            assert sink._loop.call_soon_threadsafe.call_count == 2
            sink._active_speech_pending.clear()
            for index in range(30):
                clock.return_value = 10.4 + index * 0.04
                sink.write(users[1], data)
                if not rtp_gap:
                    sink._active_speech_vads[123].is_speech.return_value = False
                    sink.write(users[0], data)
            sink._active_speech_vads[123].is_speech.return_value = True
            for index in range(10):
                clock.return_value = 11.6 + index * 0.04
                for user in users:
                    sink.write(user, data)
        assert sink._loop.call_soon_threadsafe.call_count == 3

    @pytest.mark.asyncio
    async def test_delayed_onset_does_not_interrupt_a_new_session(self, manual_control_sink):
        """A queued callback from the previous turn must not cancel a later reply."""
        sink = manual_control_sink
        user = MagicMock(id=123)
        sink._on_active_speech_detected = AsyncMock()
        sink._bot_state.current_session_id = 101
        sink._active_speech_pending.add(user.id)
        await sink._notify_active_speech_detected(user, session_id=100)
        sink._on_active_speech_detected.assert_not_awaited()
        assert user.id not in sink._active_speech_pending
        await sink._notify_active_speech_detected(user, session_id=101)
        sink._on_active_speech_detected.assert_awaited_once_with(user)

    def test_wake_word_is_finalized_after_discord_audio_gap(
        self, manual_control_sink
    ):
        user = MagicMock(spec=discord.User)
        user.id = 123
        model_name = Config.WAKE_WORD_MODEL_PATH.stem
        detector = manual_control_sink._detectors[user.id]
        detector.predict.return_value = {model_name: 1.0}
        manual_control_sink._wakeword_users[user.id] = user
        manual_control_sink._wakeword_last_audio_at[user.id] = 10.0
        manual_control_sink._wakeword_silence_chunks_sent[user.id] = 0

        manual_control_sink._finalize_wake_words_during_audio_gaps(now=10.4)

        detector.predict.assert_called_once()
        manual_control_sink._loop.call_soon_threadsafe.assert_called_once()
        assert user.id not in manual_control_sink._wakeword_last_audio_at

    def test_unmatched_audio_gap_preserves_keyword_decoder_context(
        self, manual_control_sink
    ):
        user = MagicMock(spec=discord.User)
        user.id = 123
        detector = manual_control_sink._detectors[user.id]
        detector.predict.return_value = {Config.WAKE_WORD_MODEL_PATH.stem: 0.0}
        manual_control_sink._wakeword_users[user.id] = user
        manual_control_sink._wakeword_last_audio_at[user.id] = 10.0
        manual_control_sink._wakeword_silence_chunks_sent[user.id] = 5
        manual_control_sink._user_audio_buffers[user.id].extend(b"partial")
        manual_control_sink._ww_resampled_buffers[user.id].extend(b"phrase")

        manual_control_sink._finalize_wake_words_during_audio_gaps(now=10.4)

        detector.reset.assert_not_called()
        assert manual_control_sink._user_audio_buffers[user.id] == b"partial"
        assert manual_control_sink._ww_resampled_buffers[user.id] == b"phrase"
        assert user.id not in manual_control_sink._wakeword_last_audio_at

    def test_turn_cleanup_preserves_other_users_wake_word_state(
        self, manual_control_sink
    ):
        other_user_id = 456
        manual_control_sink._detectors[123] = MagicMock()
        manual_control_sink._detectors[other_user_id] = MagicMock()
        manual_control_sink._user_audio_buffers[123].extend(b"current")
        manual_control_sink._ww_resampled_buffers[123].extend(b"current")
        manual_control_sink._user_audio_buffers[other_user_id].extend(b"other")
        manual_control_sink._ww_resampled_buffers[other_user_id].extend(b"other")

        manual_control_sink._clear_wake_word_buffers({123})

        manual_control_sink._detectors[123].reset.assert_called_once()
        manual_control_sink._detectors[other_user_id].reset.assert_not_called()
        assert manual_control_sink._user_audio_buffers[123] == b""
        assert manual_control_sink._ww_resampled_buffers[123] == b""
        assert manual_control_sink._user_audio_buffers[other_user_id] == b"other"
        assert manual_control_sink._ww_resampled_buffers[other_user_id] == b"other"

    @pytest.mark.asyncio
    async def test_cleanup_comprehensive(self, manual_control_sink):
        """Test comprehensive cleanup of all resources."""
        # Add some users and state
        with patch("src.audio.sinks.Model"):
            manual_control_sink.add_user(789)

        manual_control_sink._authority_buffer.extend(b"test_data")
        manual_control_sink._vad_raw_buffer.extend(b"vad_data")

        # Mock VAD monitor task
        mock_task = MagicMock()
        mock_task.done.return_value = False  # Task is running, so should be cancelled
        manual_control_sink._vad_monitor_task = mock_task

        manual_control_sink.cleanup()

        # Verify comprehensive cleanup
        assert len(manual_control_sink._detectors) == 0
        assert len(manual_control_sink._user_audio_buffers) == 0
        assert len(manual_control_sink._authority_buffer) == 0
        # VAD buffers are not cleared by cleanup() - only by enable_vad()
        # This is expected behavior as VAD buffers may persist across sessions
        assert manual_control_sink._vad_analyzer is None
        mock_task.cancel.assert_called_once()

    def test_wants_opus_returns_false(self, manual_control_sink):
        """Test that wants_opus returns False."""
        assert manual_control_sink.wants_opus() is False

    def test_wake_word_buffer_management(self, manual_control_sink):
        """Test wake word buffer management and processing."""
        user_id = 123

        # Set up mock detector
        mock_detector = MagicMock()
        mock_detector.predict.return_value = {"wake_word": 0.8}  # Above threshold
        manual_control_sink._detectors[user_id] = mock_detector

        # Test that user has buffers after being added
        assert user_id in manual_control_sink._ww_resampled_buffers
        assert user_id in manual_control_sink._ww_buffer_locks

        # Test buffer exists and can be written to
        initial_length = len(manual_control_sink._ww_resampled_buffers[user_id])
        manual_control_sink._ww_resampled_buffers[user_id].extend(b"test_audio")
        assert len(manual_control_sink._ww_resampled_buffers[user_id]) > initial_length


class TestManualControlSinkIntegration:
    """Integration tests for ManualControlSink with real-world scenarios."""

    @pytest.fixture
    def mock_bot_state(self):
        bot_state = MagicMock(spec=BotState)
        bot_state.current_state = BotStateEnum.STANDBY
        bot_state.current_session_id = 100
        return bot_state

    @pytest.fixture
    def mock_callbacks(self):
        return {
            "on_wake_word_detected": AsyncMock(),
            "on_vad_speech_end": AsyncMock(),
        }

    @pytest.fixture
    def mock_action_lock(self):
        return asyncio.Lock()

    @pytest.mark.asyncio
    async def test_full_push_to_talk_scenario(
        self, mock_bot_state, mock_callbacks, mock_action_lock
    ):
        """Test complete push-to-talk recording scenario."""
        with (
            patch.multiple(
                "src.audio.sinks",
                UnifiedAudioProcessor=MagicMock(),
                Config=MagicMock(),
            ),
            patch("asyncio.get_running_loop", return_value=AsyncMock()),
            patch("asyncio.create_task", return_value=MagicMock()),
        ):
            sink = ManualControlSink(
                bot_state=mock_bot_state,
                initial_consented_users={123},
                on_wake_word_detected=mock_callbacks["on_wake_word_detected"],
                on_vad_speech_end=mock_callbacks["on_vad_speech_end"],
                action_lock=mock_action_lock,
            )

            # Start push-to-talk recording
            mock_bot_state.current_state = BotStateEnum.RECORDING
            mock_bot_state.recording_method = RecordingMethod.PushToTalk
            mock_bot_state.is_authorized.return_value = True

            user = MagicMock(spec=discord.User)
            user.id = 123
            voice_data = MagicMock(spec=voice_recv.VoiceData)
            voice_data.pcm = b"audio_chunk_1"

            # Write audio data
            sink.write(user, voice_data)

            # Stop recording and get audio
            result = sink.stop_and_get_audio()

            # Should have accumulated audio in authority buffer
            assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_full_wake_word_scenario(
        self, mock_bot_state, mock_callbacks, mock_action_lock
    ):
        """Test complete wake word detection scenario."""
        with (
            patch.multiple(
                "src.audio.sinks",
                UnifiedAudioProcessor=MagicMock(),
                Config=MagicMock(),
                Model=MagicMock(),
            ),
            patch("asyncio.get_running_loop", return_value=AsyncMock()),
            patch("asyncio.create_task", return_value=MagicMock()),
        ):
            sink = ManualControlSink(
                bot_state=mock_bot_state,
                initial_consented_users={123},
                on_wake_word_detected=mock_callbacks["on_wake_word_detected"],
                on_vad_speech_end=mock_callbacks["on_vad_speech_end"],
                action_lock=mock_action_lock,
            )
            sink._loop = AsyncMock()

            # Enable VAD for wake word detection
            with patch("src.audio.sinks.VADAnalyzer"):
                sink.enable_vad(True)

            # Simulate wake word detection
            mock_bot_state.current_state = BotStateEnum.RECORDING
            mock_bot_state.recording_method = RecordingMethod.WakeWord

            user = MagicMock(spec=discord.User)
            user.id = 123
            voice_data = MagicMock(spec=voice_recv.VoiceData)
            voice_data.pcm = b"wake_word_audio"

            # Write audio data
            sink.write(user, voice_data)

            # Verify wake word processing would occur
            assert sink._is_vad_enabled is True


class TestStopKeywordGate:
    """Exercise local keyword transitions without Discord, microphones, or APIs."""

    @pytest.fixture(autouse=True)
    def isolate_detector_backend(self, monkeypatch):
        # This fixture mocks Sherpa; a developer's Paraformer .env must not
        # instantiate the real backend or change which receive path is tested.
        monkeypatch.setattr(Config, "WAKE_WORD_ENGINE", "sherpa_onnx")

    @pytest.mark.asyncio
    async def test_repeat_shut_up_dispatches_after_input_already_muted(self):
        sink = self.make_sink()
        user = MagicMock(id=123)
        with sink._user_data_lock:
            sink._handle_keyword_prediction_locked(user, {"结束": 1.0})
            first_count = sink._loop.call_soon_threadsafe.call_count
            sink._handle_keyword_prediction_locked(user, {"闭嘴": 1.0})
            sink._handle_keyword_prediction_locked(user, {"闭嘴": 1.0})
        assert sink._loop.call_soon_threadsafe.call_count == first_count + 2
        assert sink.is_user_input_blocked(123)
        sink.cleanup()

    def make_sink(self):
        state = MagicMock(spec=BotState)
        state.current_state = BotStateEnum.RECORDING
        state.current_session_id = 100
        state.authority_user_id = 123
        state.is_authorized.side_effect = lambda user: user.id == 123
        state.is_active_participant.return_value = True
        with patch("src.audio.sinks.SherpaWakeWordModel"), patch.object(ManualControlSink, "start"):
            sink = ManualControlSink(state, {123, 456}, AsyncMock(), AsyncMock(),
                                     asyncio.Lock(), on_recording_audio_chunk=AsyncMock(),
                                     on_stop_word_detected=AsyncMock())
        sink._loop = MagicMock()
        # Close queued coroutine objects: routing is tested by guild/coordinator
        # tests, while these tests exercise the synchronous receive boundary.
        sink._loop.call_soon_threadsafe.side_effect = lambda callback, coro: coro.close()
        return sink

    @pytest.mark.asyncio
    @pytest.mark.parametrize("keyword", ["闭嘴", "结束"])
    async def test_stop_blocks_only_speaker_and_wake_reopens(self, keyword):
        sink = self.make_sink()
        user = MagicMock(id=123)
        sink._authority_buffer.extend(b"unsent command")
        other_model = sink._detectors[456]
        with sink._user_data_lock:
            assert sink._handle_keyword_prediction_locked(user, {keyword: 1.0})
        assert sink.is_user_input_blocked(123)
        assert not sink.is_user_input_blocked(456)
        assert not sink._authority_buffer
        other_model.reset.assert_not_called()
        with sink._user_data_lock:
            sink._handle_keyword_prediction_locked(user, {"unrelated": 1.0})
        assert sink.is_user_input_blocked(123)
        with sink._user_data_lock:
            sink._handle_keyword_prediction_locked(user, {Config.WAKE_WORD_PHRASE: 1.0})
        assert not sink.is_user_input_blocked(123)
        sink.cleanup()

    @pytest.mark.asyncio
    async def test_detection_frame_and_muted_frames_never_schedule_audio(self):
        sink = self.make_sink()
        user = MagicMock(id=123)
        frame = MagicMock(pcm=b"\0" * Config.VAD_PROCESSING_CHUNK)
        sink._resample_and_convert = MagicMock(return_value=b"\0" * Config.WAKE_WORD_CHUNK_SIZE)
        sink._detectors[123].predict.return_value = {"闭嘴": 1.0}
        with patch("asyncio.run_coroutine_threadsafe") as submit:
            sink.write(user, frame)
            sink._detectors[123].predict.return_value = {Config.WAKE_WORD_PHRASE: 0.0}
            sink.write(user, frame)
        assert sink.is_user_input_blocked(123)
        submit.assert_not_called()
        sink.cleanup()

    @pytest.mark.asyncio
    async def test_queued_audio_stays_discarded_after_stop_then_wake(self):
        sink = self.make_sink()
        user = MagicMock(id=123)
        generation = sink.get_user_input_generation(123)
        with sink._user_data_lock:
            sink._handle_keyword_prediction_locked(user, {"结束": 1.0})
            sink._handle_keyword_prediction_locked(user, {Config.WAKE_WORD_PHRASE: 1.0})
        await sink._deliver_recording_audio_chunk(user, b"stale", generation)
        await sink._atomic_authority_buffer_update(user, b"stale", BotStateEnum.RECORDING,
                                                    True, generation)
        sink._on_recording_audio_chunk.assert_not_awaited()
        assert not sink._authority_buffer
        await sink._deliver_recording_audio_chunk(user, b"new", sink.get_user_input_generation(123))
        sink._on_recording_audio_chunk.assert_awaited_once_with(user, b"new")
        sink.cleanup()

    @pytest.mark.asyncio
    async def test_stop_finalizes_in_discord_rtp_gap_for_active_user(self):
        sink = self.make_sink()
        user = MagicMock(id=123)
        sink._wakeword_users[123] = user
        sink._wakeword_last_audio_at[123] = 10.0
        sink._detectors[123].predict.return_value = {"结束": 1.0}
        sink._finalize_wake_words_during_audio_gaps(now=10.5)
        assert sink.is_user_input_blocked(123)
        sink.cleanup()

    @pytest.mark.asyncio
    async def test_other_user_stop_preserves_authority_audio_and_gate_survives_session(self):
        sink = self.make_sink()
        sink._authority_buffer.extend(b"other user speech")
        with sink._user_data_lock:
            sink._handle_keyword_prediction_locked(MagicMock(id=456), {"结束": 1.0})
        assert sink._authority_buffer == b"other user speech"
        sink._bot_state.current_session_id = 101
        sink.update_session_id()
        sink._clear_all_wake_word_buffers()
        assert sink.is_user_input_blocked(456)
        sink.cleanup()

    @pytest.mark.asyncio
    async def test_stop_can_be_disabled(self):
        sink = self.make_sink()
        with patch.object(Config, "STOP_WORD_ENABLED", False), sink._user_data_lock:
            assert not sink._handle_keyword_prediction_locked(MagicMock(id=123), {"结束": 1.0})
        assert not sink.is_user_input_blocked(123)
        sink.cleanup()


    @pytest.mark.asyncio
    async def test_stale_queued_vad_and_speech_onset_do_not_cross_stop_wake(self):
        sink = self.make_sink()
        sink._on_active_speech_detected = AsyncMock()
        user = MagicMock(id=123)
        generation = sink.get_user_input_generation(123)
        with sink._user_data_lock:
            sink._handle_keyword_prediction_locked(user, {"结束": 1.0})
            sink._handle_keyword_prediction_locked(user, {Config.WAKE_WORD_PHRASE: 1.0})
        with patch.object(sink, "_process_vad") as process_vad:
            await sink._process_vad_async(b"old audio", 123, generation)
        await sink._notify_active_speech_detected(user, 100, generation)
        process_vad.assert_not_called()
        sink._on_active_speech_detected.assert_not_awaited()
        sink.cleanup()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("keyword", ["over", "OVER"])
    async def test_chinese_only_controls_ignore_english_over(self, keyword):
        sink = self.make_sink()
        with patch.object(Config, "STOP_WORD_PHRASES", ("闭嘴", "结束")):
            assert not sink._handle_keyword_prediction_locked(MagicMock(id=123), {keyword: 1.0})
        assert not sink.is_user_input_blocked(123)
        sink.cleanup()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("new_owner", [123, 456])
    async def test_invalidated_analyzer_cannot_clear_new_recording_buffers(self, new_owner):
        sink = self.make_sink()
        with patch("src.audio.sinks.VADAnalyzer") as analyzer_factory:
            old_analyzer, new_analyzer = MagicMock(), MagicMock()
            analyzer_factory.side_effect = [old_analyzer, new_analyzer]
            sink.enable_vad(True, silence_timeout_ms=650, grace_period_ms=200)
            old_callback = analyzer_factory.call_args.kwargs["on_speech_end"]
            sink._authority_buffer.extend(b"previous speech")

            sink.invalidate_user_input(123)
            assert sink.get_user_input_generation(123) == 1
            assert not sink.is_user_input_blocked(123)
            sink._bot_state.authority_user_id = new_owner
            sink._bot_state.current_session_id = 101
            sink.update_session_id()
            sink.enable_vad(True, silence_timeout_ms=650, grace_period_ms=200)
            new_callback = analyzer_factory.call_args.kwargs["on_speech_end"]
            sink._authority_buffer.extend(b"new owner speech")
            sink._vad_raw_buffer.extend(b"new raw")
            sink._vad_resampled_buffer.extend(b"new resampled")

            await old_callback()

            assert sink._authority_buffer == b"new owner speech"
            assert sink._vad_raw_buffer == b"new raw"
            assert sink._vad_resampled_buffer == b"new resampled"
            assert sink._vad_analyzer is new_analyzer
            assert sink._is_vad_enabled
            sink._on_vad_speech_end.assert_not_awaited()

            # The new analyzer can still finish normally with its own identity
            # and generation snapshot; stale suppression does not mute it.
            await new_callback()
            await asyncio.sleep(0)
            sink._on_vad_speech_end.assert_awaited_once_with(
                b"new owner speech", user_id=new_owner,
                input_generation=sink.get_user_input_generation(new_owner),
                session_id=101,
            )
        sink.cleanup()

@pytest.fixture
def onset_preroll_sink():
    """Exercise actual onset/routing logic without model or monitor background jobs."""
    import threading
    sink = ManualControlSink.__new__(ManualControlSink)
    sink.cleanup = lambda: None
    sink._user_data_lock = threading.RLock()
    sink._action_lock = asyncio.Lock()
    sink._active_speech_pending = set()
    sink._active_speech_latched = set()
    sink._active_speech_preroll = {123: bytearray()}
    sink._active_speech_raw_buffers = {123: bytearray()}
    sink._active_speech_resampled_buffers = {123: bytearray()}
    sink._active_speech_vads = {123: MagicMock()}
    sink._active_speech_vads[123].is_speech.return_value = True
    sink._active_speech_frame_counts = {123: 0}
    sink._active_speech_gap_counts = {123: 0}
    sink._active_speech_last_audio_at = {}
    sink._active_speech_last_triggered_at = {}
    sink._input_generations = {}
    sink._input_blocked_users = set()
    sink._authority_buffer = bytearray()
    sink._is_vad_enabled = False
    sink._audio_processor = MagicMock()
    sink._audio_processor.convert_sync.return_value = b"\0" * 1280
    sink._loop = MagicMock()
    sink._bot_state = MagicMock(
        current_state=BotStateEnum.STANDBY, current_session_id=100,
        authority_user_id=None,
    )
    sink._on_recording_audio_chunk = AsyncMock()
    sink._on_active_speech_detected = AsyncMock()
    yield sink
    # A scheduled callback is deliberately controlled by each test.
    for call in sink._loop.call_soon_threadsafe.call_args_list:
        call.args[1].close()


@pytest.mark.asyncio
async def test_active_onset_preserves_first_syllables_and_transition_audio_once(onset_preroll_sink):
    import threading
    sink = onset_preroll_sink
    sink._is_vad_enabled = True
    sink._vad_flag_lock = threading.Lock()
    sink._process_vad_async = AsyncMock()
    user = MagicMock(id=123)
    frames = [bytes([index]) * 3840 for index in range(1, 11)]
    for frame in frames:
        assert sink._process_active_speech_audio(user, frame)
    sink._loop.call_soon_threadsafe.assert_called_once()
    sink._on_recording_audio_chunk.assert_not_awaited()
    transition_frame = b"t" * 3840

    async def route(_user):
        sink._bot_state.current_state = BotStateEnum.RECORDING
        sink._bot_state.authority_user_id = user.id
        sink._bot_state.current_session_id += 1
        # Audio arriving while the routing callback is still pending must be
        # retained with the onset, even though RECORDING is already visible.
        assert sink._process_active_speech_audio(user, transition_frame, notify=False)

    sink._on_active_speech_detected.side_effect = route
    await sink._loop.call_soon_threadsafe.call_args.args[1]
    expected = b"".join(frames) + transition_frame
    sink._on_recording_audio_chunk.assert_awaited_once_with(user, expected)
    sink._process_vad_async.assert_awaited_once_with(expected, 123, 0)
    assert sink._has_received_audio_for_vad
    assert bytes(sink._authority_buffer) == expected
    assert not sink._active_speech_preroll[user.id]
    assert user.id not in sink._active_speech_pending
    assert not sink._process_active_speech_audio(user, b"n" * 3840, notify=False)
    sink._on_recording_audio_chunk.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", ["other_owner", "stop", "new_generation"])
async def test_onset_preroll_not_uploaded_after_routing_rejection(onset_preroll_sink, reject):
    sink = onset_preroll_sink
    user = MagicMock(id=123)
    for _ in range(10):
        sink._process_active_speech_audio(user, b"a" * 3840)

    async def route(_user):
        sink._bot_state.current_state = BotStateEnum.RECORDING
        sink._bot_state.current_session_id += 1
        sink._bot_state.authority_user_id = 456 if reject == "other_owner" else 123
        if reject == "stop":
            sink._input_blocked_users.add(123)
        if reject == "new_generation":
            sink._input_generations[123] = 1

    sink._on_active_speech_detected.side_effect = route
    await sink._loop.call_soon_threadsafe.call_args.args[1]
    sink._on_recording_audio_chunk.assert_not_awaited()
    assert not sink._authority_buffer
    assert not sink._active_speech_preroll[123]


def test_active_onset_preroll_is_bounded_and_reset_with_gate_state(onset_preroll_sink):
    sink = onset_preroll_sink
    sink._active_speech_vads[123].is_speech.return_value = False
    user = MagicMock(id=123)
    for _ in range(100):
        sink._process_active_speech_audio(user, b"a" * 3840)
    assert len(sink._active_speech_preroll[123]) == 48000 * 2 * Config.SAMPLE_WIDTH
    sink._reset_active_speech_vad_locked(123)
    assert not sink._active_speech_preroll[123]
