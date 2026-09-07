"""
Tests for GuildSession - The main session orchestrator.

These tests verify that GuildSession correctly manages per-guild state,
coordinates between components, handles user interactions, and provides
proper cleanup and error handling.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest
import discord
from discord.ext import commands

from src.bot.session.guild_session import GuildSession
from src.bot.state import BotStateEnum, RecordingMethod
from src.ai_services.interface import ProviderCapabilities
from src.audio.sinks import ManualControlSink
from src.config.config import Config


class TestGuildSessionLiveAudioStreaming:
    """Verify microphone pacing and explicit stream finalization."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("provider_format", "chunk_bytes"),
        [((16000, 1), 3200), ((48000, 2), 19200)],
    )
    async def test_stream_loop_sends_audio_then_silence_for_server_vad(
        self, provider_format, chunk_bytes
    ):
        session = GuildSession.__new__(GuildSession)
        session._live_audio_queue = asyncio.Queue(maxsize=10)
        session._live_audio_buffer = bytearray()
        session._live_input_active = True
        session._live_input_user_id = 42
        session._live_input_user_name = "Alice"
        session.guild = MagicMock(id=123)
        session.ai_coordinator = MagicMock()
        session.ai_coordinator.get_processing_audio_format.return_value = (
            provider_format
        )

        two_chunks_sent = asyncio.Event()

        async def send_chunk(*_args):
            if session._send_live_provider_chunk.await_count >= 2:
                two_chunks_sent.set()
            return True

        session._send_live_provider_chunk = AsyncMock(side_effect=send_chunk)
        await session._live_audio_queue.put((42, "Alice", b"\x01" * chunk_bytes))

        task = asyncio.create_task(session._live_audio_stream_loop())
        try:
            await asyncio.wait_for(two_chunks_sent.wait(), timeout=1)
        finally:
            task.cancel()
            await task

        first = session._send_live_provider_chunk.await_args_list[0].args
        second = session._send_live_provider_chunk.await_args_list[1].args
        assert first == (42, "Alice", b"\x01" * chunk_bytes)
        assert second == (42, "Alice", b"\x00" * chunk_bytes)

    @pytest.mark.asyncio
    async def test_force_finish_flushes_and_finalizes_live_stream(self):
        session = GuildSession.__new__(GuildSession)
        session._live_input_active = True
        session._live_input_user_id = 42
        session._live_input_user_name = "Alice"
        session._live_audio_queue = asyncio.Queue(maxsize=10)
        session._live_audio_buffer = bytearray(b"\x01" * 100)
        session._send_live_provider_chunk = AsyncMock(return_value=True)
        session.ai_coordinator = MagicMock()
        session.ai_coordinator.get_processing_audio_format.return_value = (16000, 1)
        session.ai_coordinator.finalize_audio_stream = AsyncMock(return_value=True)
        session._audio_processor = MagicMock()
        session.bot_state = MagicMock(current_session_id=7)
        session._agent_response_pending = False
        session._response_playback_seen = True
        session._response_pending_since = 0.0

        assert await session._finish_live_audio_input(finalize=True, flush=True)

        sent = session._send_live_provider_chunk.await_args.args
        assert sent[:2] == (42, "Alice")
        assert sent[2][:100] == b"\x01" * 100
        assert sent[2][100:] == b"\x00" * 3100
        session.ai_coordinator.finalize_audio_stream.assert_awaited_once()
        assert session._agent_response_pending is True
        assert session._response_playback_seen is False
        assert session._live_input_active is False

    @pytest.mark.asyncio
    async def test_failed_tail_upload_does_not_finalize(self):
        session = GuildSession.__new__(GuildSession)
        session._live_input_active = True
        session._live_input_user_id = 42
        session._live_input_user_name = "Alice"
        session._live_audio_queue = asyncio.Queue()
        session._live_audio_buffer = bytearray(b"tail")
        session._send_live_provider_chunk = AsyncMock(return_value=False)
        session.ai_coordinator = MagicMock()
        session.ai_coordinator.get_processing_audio_format.return_value = (16000, 1)
        session.ai_coordinator.finalize_audio_stream = AsyncMock()
        session._audio_processor = MagicMock()
        session.bot_state = MagicMock(current_session_id=1)

        assert not await session._finish_live_audio_input(finalize=True, flush=True)
        session.ai_coordinator.finalize_audio_stream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shut_up_cancels_output_but_closes_only_owning_input(self):
        for owns_turn in [True, False]:
            session = GuildSession.__new__(GuildSession)
            session._action_lock = asyncio.Lock()
            session._audio_sink = MagicMock()
            session._audio_sink.is_user_input_blocked.return_value = True
            session.bot_state = MagicMock(current_state=BotStateEnum.RECORDING)
            session.bot_state.remove_active_participant = AsyncMock()
            session.bot_state.stop_recording = AsyncMock()
            session.conversation_router = MagicMock()
            session.ai_coordinator = MagicMock()
            session.ai_coordinator.end_conversation = AsyncMock()
            session._current_turn_user_id = 42 if owns_turn else 7
            session._live_input_user_id = session._current_turn_user_id
            session._live_input_active = True
            session._finish_live_audio_input = AsyncMock()
            session._interrupt_ongoing_playback = AsyncMock()

            await session.on_stop_word_detected(MagicMock(id=42), "闭嘴")
            session.ai_coordinator.end_conversation.assert_awaited_once_with(reason="explicit_stop")

            session.bot_state.remove_active_participant.assert_awaited_once_with(42)
            session.conversation_router.remove_participant.assert_called_once_with(42)
            assert session._interrupt_ongoing_playback.await_count == 1
            assert session._finish_live_audio_input.await_count == int(owns_turn)
            assert session.bot_state.stop_recording.await_count == int(owns_turn)

    @pytest.mark.asyncio
    async def test_new_wake_supersedes_queued_stop(self):
        session = GuildSession.__new__(GuildSession)
        session._action_lock = asyncio.Lock()
        session._audio_sink = MagicMock()
        session._audio_sink.is_user_input_blocked.return_value = False
        session.bot_state = AsyncMock()
        await session.on_stop_word_detected(MagicMock(id=42), "over")
        session.bot_state.remove_active_participant.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_queued_audio_generation_before_stop_is_discarded_after_wake(self):
        session = GuildSession.__new__(GuildSession)
        session._live_audio_queue = asyncio.Queue()
        session._live_audio_buffer = bytearray(b"stale partial")
        session._live_input_active = True
        session._live_input_user_id = 42
        session._live_input_user_name = "Alice"
        session._live_input_generation = 1
        session._audio_sink = MagicMock()
        session._audio_sink.get_user_input_generation.return_value = 3
        session.guild = MagicMock(id=123)
        session.ai_coordinator = MagicMock()
        session.ai_coordinator.get_processing_audio_format.return_value = (16000, 1)
        sent = asyncio.Event()

        async def send(*_args):
            sent.set()
            return True

        session._send_live_provider_chunk = AsyncMock(side_effect=send)
        session._live_audio_queue.put_nowait((42, "Alice", b"old" * 3200, 1))
        session._live_audio_queue.put_nowait((42, "Alice", b"\x02" * 3200, 3))
        task = asyncio.create_task(session._live_audio_stream_loop())
        try:
            await asyncio.wait_for(sent.wait(), timeout=1)
        finally:
            task.cancel()
            await task
        assert session._send_live_provider_chunk.await_args_list[0].args[2] == b"\x02" * 3200

    @pytest.mark.asyncio
    async def test_failed_old_upload_cannot_close_restarted_stream(self):
        session = GuildSession.__new__(GuildSession)
        session._live_audio_queue = asyncio.Queue()
        session._live_audio_buffer = bytearray()
        session._live_input_active = True
        session._live_input_user_id = 42
        session._live_input_user_name = "Alice"
        session._live_stream_revision = 1
        session.guild = MagicMock(id=123)
        session.ai_coordinator = MagicMock()
        session.ai_coordinator.get_processing_audio_format.return_value = (16000, 1)
        sent = asyncio.Event()

        async def send(*_args):
            if session._send_live_provider_chunk.await_count == 1:
                session._live_stream_revision = 2
                session._live_audio_queue.put_nowait((42, "Alice", b"\x02" * 3200, 0))
                return False
            sent.set()
            return True

        session._send_live_provider_chunk = AsyncMock(side_effect=send)
        session._live_audio_queue.put_nowait((42, "Alice", b"\x01" * 3200, 0))
        task = asyncio.create_task(session._live_audio_stream_loop())
        try:
            await asyncio.wait_for(sent.wait(), timeout=1)
        finally:
            task.cancel()
            await task
        assert session._live_input_active
        assert session._send_live_provider_chunk.await_args_list[1].args[2] == b"\x02" * 3200


class TestGuildSessionInitialization:
    """Test GuildSession initialization and basic functionality."""

    @pytest.fixture
    def mock_guild(self):
        guild = MagicMock(spec=discord.Guild)
        guild.id = 123
        guild.name = "Test Guild"
        return guild

    @pytest.fixture
    def mock_bot(self):
        bot = AsyncMock(spec=commands.Bot)
        bot.user = MagicMock()
        bot.user.id = 456
        return bot

    @pytest.fixture
    def ai_service_factories(self):
        return {
            "openai": ("OpenAIServiceManager", {"api_key": "test_key"}),
            "gemini": ("GeminiServiceManager", {"api_key": "test_key"}),
        }

    @patch("src.bot.session.guild_session.SessionUIManager")
    @patch("src.bot.session.guild_session.AudioPlaybackManager")
    @patch("src.bot.session.guild_session.VoiceConnectionManager")
    @patch("src.bot.session.guild_session.AIServiceCoordinator")
    @patch("src.bot.session.guild_session.InteractionHandler")
    @patch("src.bot.session.guild_session.UnifiedAudioProcessor")
    @patch("src.bot.session.guild_session.BotState")
    def test_initialization(
        self,
        mock_bot_state,
        mock_audio_processor,
        mock_interaction_handler,
        mock_ai_coordinator,
        mock_voice_connection,
        mock_audio_playback,
        mock_ui_manager,
        mock_guild,
        mock_bot,
        ai_service_factories,
    ):
        """Test that GuildSession initializes all components correctly."""
        session = GuildSession(mock_guild, mock_bot, ai_service_factories)

        assert session.guild == mock_guild
        assert session.bot == mock_bot
        assert session._audio_sink is None
        assert isinstance(session._background_tasks, set)
        assert len(session._background_tasks) == 0

    @patch("src.bot.session.guild_session.SessionUIManager")
    @patch("src.bot.session.guild_session.AudioPlaybackManager")
    @patch("src.bot.session.guild_session.VoiceConnectionManager")
    @patch("src.bot.session.guild_session.AIServiceCoordinator")
    @patch("src.bot.session.guild_session.InteractionHandler")
    @patch("src.bot.session.guild_session.UnifiedAudioProcessor")
    @patch("src.bot.session.guild_session.BotState")
    def test_component_initialization_order(
        self,
        mock_bot_state,
        mock_audio_processor,
        mock_interaction_handler,
        mock_ai_coordinator,
        mock_voice_connection,
        mock_audio_playback,
        mock_ui_manager,
        mock_guild,
        mock_bot,
        ai_service_factories,
    ):
        """Test that components are initialized in the correct order."""
        session = GuildSession(mock_guild, mock_bot, ai_service_factories)

        # Verify InteractionHandler was initialized last with fully initialized GuildSession
        mock_interaction_handler.assert_called_once()
        call_args = mock_interaction_handler.call_args
        assert call_args[1]["guild_session"] == session


class TestGuildSessionLifecycle:
    """Test session lifecycle methods."""

    @pytest.fixture
    def guild_session_with_mocks(self):
        """Create a GuildSession with mocked dependencies."""
        guild = MagicMock(spec=discord.Guild)
        guild.id = 123
        bot = AsyncMock(spec=commands.Bot)
        ai_service_factories = {"openai": ("OpenAIServiceManager", {"api_key": "test"})}

        with patch.multiple(
            "src.bot.session.guild_session",
            SessionUIManager=MagicMock(),
            AudioPlaybackManager=MagicMock(),
            VoiceConnectionManager=MagicMock(),
            AIServiceCoordinator=MagicMock(),
            InteractionHandler=MagicMock(),
            UnifiedAudioProcessor=MagicMock(),
            BotState=MagicMock(),
        ):
            session = GuildSession(guild, bot, ai_service_factories)

            # Set up commonly used mocks
            session.ui_manager = AsyncMock()
            session.interaction_handler = AsyncMock()
            session.ai_coordinator = AsyncMock()
            session.voice_connection = AsyncMock()
            session.bot_state = AsyncMock()

            return session

    @pytest.mark.asyncio
    async def test_start_background_tasks(self, guild_session_with_mocks):
        """Test that background tasks are started correctly."""
        session = guild_session_with_mocks

        await session.start_background_tasks()

        session.ui_manager.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_success(self, guild_session_with_mocks):
        """Test successful cleanup of all resources."""
        session = guild_session_with_mocks

        # Add some background tasks (create proper async task mocks)
        async def mock_coroutine():
            pass

        mock_task1 = asyncio.create_task(mock_coroutine())
        mock_task2 = asyncio.create_task(mock_coroutine())
        session._background_tasks.add(mock_task1)
        session._background_tasks.add(mock_task2)

        # Add an audio sink
        mock_audio_sink = MagicMock()
        session._audio_sink = mock_audio_sink

        session.voice_connection.is_connected.return_value = True

        await session.cleanup()

        # Verify cleanup sequence (tasks are cancelled but we can't easily mock the cancel calls)
        session.ui_manager.cleanup.assert_called_once()
        session.interaction_handler.cleanup.assert_called_once()
        mock_audio_sink.cleanup.assert_called_once()
        session.ai_coordinator.shutdown.assert_called_once()
        session.voice_connection.disconnect.assert_called_once()
        session.bot_state.reset_to_idle.assert_called_once()

        # Audio sink should be cleared
        assert session._audio_sink is None

    @pytest.mark.asyncio
    async def test_cleanup_handles_ai_coordinator_exception(
        self, guild_session_with_mocks
    ):
        """Test cleanup handles AI coordinator shutdown exceptions."""
        session = guild_session_with_mocks

        session.ai_coordinator.shutdown.side_effect = Exception("AI shutdown failed")
        session.voice_connection.is_connected.return_value = False

        # Should not raise exception
        await session.cleanup()

        session.bot_state.reset_to_idle.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_handles_voice_disconnect_exception(
        self, guild_session_with_mocks
    ):
        """Test cleanup handles voice disconnect exceptions."""
        session = guild_session_with_mocks

        session.voice_connection.is_connected.return_value = True
        session.voice_connection.disconnect.side_effect = Exception("Disconnect failed")

        # Should not raise exception
        await session.cleanup()

        session.bot_state.reset_to_idle.assert_called_once()


class TestGuildSessionEventHandlers:
    """Test event handling methods."""

    @pytest.fixture
    def guild_session_with_mocks(self):
        guild = MagicMock(spec=discord.Guild)
        guild.id = 123
        bot = AsyncMock(spec=commands.Bot)
        ai_service_factories = {"openai": ("OpenAIServiceManager", {"api_key": "test"})}

        with patch.multiple(
            "src.bot.session.guild_session",
            SessionUIManager=MagicMock(),
            AudioPlaybackManager=MagicMock(),
            VoiceConnectionManager=MagicMock(),
            AIServiceCoordinator=MagicMock(),
            InteractionHandler=MagicMock(),
            UnifiedAudioProcessor=MagicMock(),
            BotState=MagicMock(),
        ):
            session = GuildSession(guild, bot, ai_service_factories)
            session.interaction_handler = AsyncMock()
            session.ai_coordinator = AsyncMock()
            session.bot_state = AsyncMock()
            return session

    @pytest.mark.asyncio
    async def test_handle_reaction_add(self, guild_session_with_mocks):
        """Test reaction add event delegation."""
        session = guild_session_with_mocks
        reaction = MagicMock(spec=discord.Reaction)
        user = MagicMock(spec=discord.User)

        await session.handle_reaction_add(reaction, user)

        session.interaction_handler.handle_reaction_add.assert_called_once_with(
            reaction, user
        )

    @pytest.mark.asyncio
    async def test_handle_reaction_remove(self, guild_session_with_mocks):
        """Test reaction remove event delegation."""
        session = guild_session_with_mocks
        reaction = MagicMock(spec=discord.Reaction)
        user = MagicMock(spec=discord.User)

        await session.handle_reaction_remove(reaction, user)

        session.interaction_handler.handle_reaction_remove.assert_called_once_with(
            reaction, user
        )

    @pytest.mark.asyncio
    async def test_handle_voice_connection_update_connected_ai_connected(
        self, guild_session_with_mocks
    ):
        """Test voice connection update when both voice and AI are connected."""
        session = guild_session_with_mocks
        session.ai_coordinator.is_connected.return_value = True

        await session.handle_voice_connection_update(True)

        session.bot_state.recover_to_standby.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_voice_connection_update_connected_ai_disconnected(
        self, guild_session_with_mocks
    ):
        """Test voice connection update when voice connected but AI disconnected."""
        session = guild_session_with_mocks
        # Fix: Make is_connected a sync method that returns False
        session.ai_coordinator.is_connected = MagicMock(return_value=False)

        await session.handle_voice_connection_update(True)

        # Should not call recover_to_standby when AI is not connected
        session.bot_state.recover_to_standby.assert_not_called()
        # Should also not enter error state since voice is connected
        session.bot_state.enter_connection_error_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_voice_connection_update_disconnected(
        self, guild_session_with_mocks
    ):
        """Test voice connection update when voice disconnected."""
        session = guild_session_with_mocks

        await session.handle_voice_connection_update(False)

        session.bot_state.enter_connection_error_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_ai_connect(self, guild_session_with_mocks):
        """Test AI connect callback."""
        session = guild_session_with_mocks
        session.voice_connection = AsyncMock()
        session.voice_connection.is_connected.return_value = True

        await session._on_ai_connect()

        session.bot_state.recover_to_standby.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_ai_connect_no_voice(self, guild_session_with_mocks):
        """Test AI connect callback when voice not connected."""
        session = guild_session_with_mocks
        session.voice_connection = AsyncMock()
        # Fix: Make is_connected a sync method that returns False
        session.voice_connection.is_connected = MagicMock(return_value=False)

        await session._on_ai_connect()

        session.bot_state.recover_to_standby.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_ai_disconnect(self, guild_session_with_mocks):
        """Test AI disconnect callback."""
        session = guild_session_with_mocks

        await session._on_ai_disconnect()

        session.bot_state.enter_connection_error_state.assert_called_once()


class TestGuildSessionUserInteractions:
    """Test user interaction handling methods."""

    @pytest.fixture
    def guild_session_with_mocks(self):
        guild = MagicMock(spec=discord.Guild)
        guild.id = 123
        bot = AsyncMock(spec=commands.Bot)
        ai_service_factories = {"openai": ("OpenAIServiceManager", {"api_key": "test"})}

        with patch.multiple(
            "src.bot.session.guild_session",
            SessionUIManager=MagicMock(),
            AudioPlaybackManager=MagicMock(),
            VoiceConnectionManager=MagicMock(),
            AIServiceCoordinator=MagicMock(),
            InteractionHandler=MagicMock(),
            UnifiedAudioProcessor=MagicMock(),
            BotState=MagicMock(),
        ):
            session = GuildSession(guild, bot, ai_service_factories)
            session.bot_state = AsyncMock()
            session.ui_manager = AsyncMock()
            return session

    @pytest.mark.asyncio
    async def test_handle_consent_reaction_added(self, guild_session_with_mocks):
        """Test handling consent reaction when added."""
        session = guild_session_with_mocks
        user = MagicMock(spec=discord.User)
        user.id = 789

        # Mock audio sink
        mock_audio_sink = MagicMock()
        session._audio_sink = mock_audio_sink

        await session.handle_consent_reaction(user, added=True)

        session.bot_state.grant_consent.assert_called_once_with(user.id)
        session.ui_manager.schedule_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_consent_reaction_removed(self, guild_session_with_mocks):
        """Test handling consent reaction when removed."""
        session = guild_session_with_mocks
        user = MagicMock(spec=discord.User)
        user.id = 789

        # Mock audio sink
        mock_audio_sink = MagicMock()
        session._audio_sink = mock_audio_sink

        await session.handle_consent_reaction(user, added=False)

        session.bot_state.revoke_consent.assert_called_once_with(user.id)
        mock_audio_sink.remove_user.assert_called_once_with(user.id)
        session.ui_manager.schedule_update.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.bot.session.guild_session.Config.VOICE_ACCESS_MODE", "implicit")
    async def test_implicit_member_join_grants_voice_access(
        self, guild_session_with_mocks
    ):
        session = guild_session_with_mocks
        member = MagicMock(spec=discord.Member)
        member.id = 789
        member.bot = False
        session._audio_sink = None

        await session.handle_voice_member_access(member, joined=True)

        session.bot_state.grant_consent.assert_awaited_once_with(member.id)
        session.ui_manager.schedule_update.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.bot.session.guild_session.Config.VOICE_ACCESS_MODE", "implicit")
    async def test_implicit_member_leave_revokes_voice_access(
        self, guild_session_with_mocks
    ):
        session = guild_session_with_mocks
        member = MagicMock(spec=discord.Member)
        member.id = 789
        member.bot = False
        mock_audio_sink = MagicMock()
        session._audio_sink = mock_audio_sink

        await session.handle_voice_member_access(member, joined=False)

        session.bot_state.revoke_consent.assert_awaited_once_with(member.id)
        mock_audio_sink.remove_user.assert_called_once_with(member.id)
        session.ui_manager.schedule_update.assert_called_once()


class TestGuildSessionPushToTalkInteractions:
    """Test push-to-talk interaction handling."""

    @pytest.fixture
    def guild_session_with_mocks(self):
        guild = MagicMock(spec=discord.Guild)
        guild.id = 123
        bot = AsyncMock(spec=commands.Bot)
        ai_service_factories = {"openai": ("OpenAIServiceManager", {"api_key": "test"})}

        with patch.multiple(
            "src.bot.session.guild_session",
            SessionUIManager=MagicMock(),
            AudioPlaybackManager=MagicMock(),
            VoiceConnectionManager=MagicMock(),
            AIServiceCoordinator=MagicMock(),
            InteractionHandler=MagicMock(),
            UnifiedAudioProcessor=MagicMock(),
            BotState=MagicMock(),
            ManualControlSink=MagicMock(),
        ):
            session = GuildSession(guild, bot, ai_service_factories)
            session.bot_state = AsyncMock()
            session.voice_connection = AsyncMock()
            session.ai_coordinator = AsyncMock()
            return session

    @pytest.mark.asyncio
    async def test_handle_pushtotalk_reaction_stop_recording(
        self, guild_session_with_mocks
    ):
        """Test push-to-talk reaction to stop recording."""
        session = guild_session_with_mocks
        user = MagicMock(spec=discord.User)

        # Set up conditions for stopping recording
        session.bot_state.current_state = BotStateEnum.RECORDING
        session.bot_state.recording_method = RecordingMethod.PushToTalk
        session.bot_state.is_authorized.return_value = True

        # Mock audio sink
        mock_audio_sink = MagicMock()
        mock_audio_sink.stop_and_get_audio.return_value = b"audio_data"
        session._audio_sink = mock_audio_sink

        # Mock handle finished recording
        session._handle_finished_recording = MagicMock()

        await session.handle_pushtotalk_reaction(user, added=False)

        mock_audio_sink.stop_and_get_audio.assert_called_once()
        session._handle_finished_recording.assert_called_once_with(b"audio_data")
        session.bot_state.stop_recording.assert_called_once()

    @pytest.mark.asyncio
    async def test_pushtotalk_opens_upload_gate(self, guild_session_with_mocks):
        session = guild_session_with_mocks
        user = MagicMock(spec=discord.User)
        user.id = 456
        session.bot_state.current_state = BotStateEnum.STANDBY
        session.ai_coordinator.cancel_ongoing_response.return_value = True
        session._audio_sink = MagicMock()

        await session.handle_pushtotalk_reaction(user, added=True)

        session.bot_state.add_active_participant.assert_awaited_once_with(456)
        session.bot_state.start_recording.assert_awaited_once_with(
            user, RecordingMethod.PushToTalk
        )

    @pytest.mark.asyncio
    async def test_interrupt_ongoing_playback(self, guild_session_with_mocks):
        """Test interrupting ongoing playback."""
        session = guild_session_with_mocks
        session.ai_coordinator.cancel_ongoing_response.return_value = True

        await session._interrupt_ongoing_playback()

        session.voice_connection.stop_playback.assert_called_once()
        session.ai_coordinator.cancel_ongoing_response.assert_called_once()
        session.audio_playback_manager.interrupt_audio_stream.assert_called_once()

    @pytest.mark.asyncio
    async def test_interrupt_ongoing_playback_cancel_fails(
        self, guild_session_with_mocks
    ):
        """Test interrupting ongoing playback when cancel fails."""
        session = guild_session_with_mocks
        session.ai_coordinator.cancel_ongoing_response.return_value = False

        await session._interrupt_ongoing_playback()

        session.voice_connection.stop_playback.assert_called_once()


class TestGuildSessionWakeWordInteractions:
    """Test wake word detection interaction handling."""

    @pytest.fixture
    def guild_session_with_mocks(self):
        guild = MagicMock(spec=discord.Guild)
        guild.id = 123
        bot = AsyncMock(spec=commands.Bot)
        ai_service_factories = {"openai": ("OpenAIServiceManager", {"api_key": "test"})}

        with patch.multiple(
            "src.bot.session.guild_session",
            SessionUIManager=MagicMock(),
            AudioPlaybackManager=MagicMock(),
            VoiceConnectionManager=MagicMock(),
            AIServiceCoordinator=MagicMock(),
            InteractionHandler=MagicMock(),
            UnifiedAudioProcessor=MagicMock(),
            BotState=MagicMock(),
            ManualControlSink=MagicMock(),
        ):
            session = GuildSession(guild, bot, ai_service_factories)
            session.bot_state = AsyncMock()
            session.audio_playback_manager = AsyncMock()
            return session

    @pytest.mark.asyncio
    async def test_on_wake_word_detected_wrong_state(self, guild_session_with_mocks):
        """Test wake word detection in wrong state."""
        session = guild_session_with_mocks
        user = MagicMock(spec=discord.User)

        # Wrong state - already recording
        session.bot_state.current_state = BotStateEnum.RECORDING
        session._current_turn_user_id = user.id

        await session.on_wake_word_detected(user)

        # Should not start recording
        session.bot_state.start_recording.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_user_can_be_admitted_during_another_recording(
        self, guild_session_with_mocks
    ):
        session = guild_session_with_mocks
        user = MagicMock(spec=discord.User)
        user.id = 456
        session.bot_state.current_state = BotStateEnum.RECORDING
        session.bot_state.is_active_participant = MagicMock(return_value=False)
        session.conversation_router.cross_user_wake_required = False
        session._is_agent_speaking = MagicMock(return_value=False)
        session.conversation_router.route_speech = MagicMock()

        await session.on_wake_word_detected(user)

        session.conversation_router.route_speech.assert_called_once_with(
            user.id,
            via_wake_word=True,
            agent_speaking=False,
        )
        session.bot_state.add_active_participant.assert_awaited_once_with(user.id)
        session.bot_state.start_recording.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_wake_word_does_not_play_conflicting_start_cue(
        self, guild_session_with_mocks
    ):
        session = guild_session_with_mocks
        user = MagicMock(spec=discord.User)
        session.bot_state.current_state = BotStateEnum.STANDBY
        session._begin_routed_recording = AsyncMock(return_value=True)
        session._live_input_active = True

        await session.on_wake_word_detected(user)

        session.audio_playback_manager.play_cue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_buffered_wake_word_keeps_start_cue(self, guild_session_with_mocks):
        session = guild_session_with_mocks
        user = MagicMock(spec=discord.User)
        session.bot_state.current_state = BotStateEnum.STANDBY
        session._begin_routed_recording = AsyncMock(return_value=True)
        session._live_input_active = False

        await session.on_wake_word_detected(user)

        session.audio_playback_manager.play_cue.assert_awaited_once_with(
            "start_recording"
        )

    @pytest.mark.asyncio
    async def test_on_vad_speech_end_valid_conditions(self, guild_session_with_mocks):
        """Test VAD speech end with valid conditions."""
        session = guild_session_with_mocks
        audio_data = b"test_audio_data"

        # Set up valid conditions
        session.bot_state.current_state = BotStateEnum.RECORDING
        session.bot_state.recording_method = RecordingMethod.WakeWord

        # Mock handle finished recording
        session._handle_finished_recording = MagicMock()

        await session.on_vad_speech_end(audio_data)

        session._handle_finished_recording.assert_called_once_with(audio_data)
        session.bot_state.stop_recording.assert_called_once()

    @pytest.mark.asyncio
    async def test_local_vad_safety_timeout_finalizes_live_turn(
        self, guild_session_with_mocks
    ):
        session = guild_session_with_mocks
        session.bot_state.current_state = BotStateEnum.RECORDING
        session.bot_state.recording_method = RecordingMethod.WakeWord
        session._live_input_active = True
        session._finish_live_audio_input = AsyncMock(return_value=True)
        session._handle_finished_recording = MagicMock()
        session.conversation_router.release_participants = MagicMock()

        await session.on_vad_speech_end(b"already-streamed-audio")

        session._finish_live_audio_input.assert_awaited_once_with(
            finalize=True, flush=True
        )
        session.bot_state.stop_recording.assert_awaited_once()
        session.conversation_router.release_participants.assert_not_called()
        session.bot_state.clear_active_participants.assert_not_awaited()
        session._handle_finished_recording.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_vad_speech_end_wrong_state(self, guild_session_with_mocks):
        """Test VAD speech end in wrong state."""
        session = guild_session_with_mocks
        audio_data = b"test_audio_data"

        # Wrong state
        session.bot_state.current_state = BotStateEnum.STANDBY
        session.bot_state.recording_method = RecordingMethod.WakeWord

        session._handle_finished_recording = MagicMock()

        await session.on_vad_speech_end(audio_data)

        # Should not handle recording
        session._handle_finished_recording.assert_not_called()


class TestGuildSessionSessionManagement:
    """Test session management operations."""

    @pytest.fixture
    def guild_session_with_mocks(self):
        guild = MagicMock(spec=discord.Guild)
        guild.id = 123
        bot = AsyncMock(spec=commands.Bot)
        ai_service_factories = {"openai": ("OpenAIServiceManager", {"api_key": "test"})}

        with patch.multiple(
            "src.bot.session.guild_session",
            SessionUIManager=MagicMock(),
            AudioPlaybackManager=MagicMock(),
            VoiceConnectionManager=MagicMock(),
            AIServiceCoordinator=MagicMock(),
            InteractionHandler=MagicMock(),
            UnifiedAudioProcessor=MagicMock(),
            BotState=MagicMock(),
        ):
            session = GuildSession(guild, bot, ai_service_factories)
            session.bot_state = AsyncMock()
            session.ai_coordinator = AsyncMock()
            session.voice_connection = AsyncMock()
            session.ui_manager = AsyncMock()
            return session

    @pytest.mark.asyncio
    async def test_set_provider_success(self, guild_session_with_mocks):
        """Test successful AI provider change."""
        session = guild_session_with_mocks

        mock_ctx = AsyncMock(spec=commands.Context)
        session.ai_coordinator.switch_provider = AsyncMock(return_value=True)
        session.voice_connection.is_connected = MagicMock(return_value=True)
        session.ai_coordinator.ai_service_factories = {
            "openai": ("test", {}),
            "gemini": ("test", {}),
        }

        await session.set_provider(mock_ctx, "openai")

        session.ai_coordinator.switch_provider.assert_called_once()
        mock_ctx.send.assert_called_once_with("AI provider switched to 'OPENAI'.")

    @pytest.mark.asyncio
    async def test_set_provider_invalid(self, guild_session_with_mocks):
        """Test AI provider change with invalid provider."""
        session = guild_session_with_mocks

        mock_ctx = AsyncMock(spec=commands.Context)
        session.ai_coordinator.ai_service_factories = {
            "openai": ("test", {}),
            "gemini": ("test", {}),
        }

        await session.set_provider(mock_ctx, "invalid_provider")

        mock_ctx.send.assert_called_once_with(
            "Invalid provider name 'invalid_provider'. Valid options are: openai, gemini."
        )


class TestGuildSessionConcurrency:
    """Test concurrency and action lock behavior."""

    @pytest.fixture
    def guild_session_with_mocks(self):
        guild = MagicMock(spec=discord.Guild)
        guild.id = 123
        bot = AsyncMock(spec=commands.Bot)
        ai_service_factories = {"openai": ("OpenAIServiceManager", {"api_key": "test"})}

        with patch.multiple(
            "src.bot.session.guild_session",
            SessionUIManager=MagicMock(),
            AudioPlaybackManager=MagicMock(),
            VoiceConnectionManager=MagicMock(),
            AIServiceCoordinator=MagicMock(),
            InteractionHandler=MagicMock(),
            UnifiedAudioProcessor=MagicMock(),
            BotState=MagicMock(),
        ):
            session = GuildSession(guild, bot, ai_service_factories)
            session.bot_state = AsyncMock()
            session.ui_manager = AsyncMock()
            return session

    @pytest.mark.asyncio
    async def test_concurrent_consent_reactions(self, guild_session_with_mocks):
        """Test that concurrent consent reactions are properly serialized."""
        session = guild_session_with_mocks

        user1 = MagicMock(spec=discord.User)
        user1.id = 111
        user2 = MagicMock(spec=discord.User)
        user2.id = 222

        # Start both tasks concurrently
        task1 = asyncio.create_task(session.handle_consent_reaction(user1, True))
        task2 = asyncio.create_task(session.handle_consent_reaction(user2, True))

        # Wait for both to complete
        await asyncio.gather(task1, task2)

        # Both should have been processed
        assert session.bot_state.grant_consent.call_count == 2
        session.bot_state.grant_consent.assert_has_calls(
            [call(111), call(222)], any_order=True
        )

    @pytest.mark.asyncio
    async def test_concurrent_pushtotalk_reactions(self, guild_session_with_mocks):
        """Test that concurrent push-to-talk reactions are properly serialized."""
        session = guild_session_with_mocks

        user = MagicMock(spec=discord.User)
        session.bot_state.current_state = BotStateEnum.STANDBY

        # Mock interrupt method
        session._interrupt_ongoing_playback = AsyncMock()

        # Start concurrent push-to-talk events
        task1 = asyncio.create_task(session.handle_pushtotalk_reaction(user, True))
        task2 = asyncio.create_task(session.handle_pushtotalk_reaction(user, True))

        # Wait for both to complete
        await asyncio.gather(task1, task2)

        # Should be properly serialized - exact behavior depends on timing
        # but both tasks should complete without race conditions
        assert session._interrupt_ongoing_playback.call_count >= 1


class TestGuildSessionErrorHandling:
    """Test error handling and edge cases."""

    @pytest.fixture
    def guild_session_with_mocks(self):
        guild = MagicMock(spec=discord.Guild)
        guild.id = 123
        bot = AsyncMock(spec=commands.Bot)
        ai_service_factories = {"openai": ("OpenAIServiceManager", {"api_key": "test"})}

        with patch.multiple(
            "src.bot.session.guild_session",
            SessionUIManager=MagicMock(),
            AudioPlaybackManager=MagicMock(),
            VoiceConnectionManager=MagicMock(),
            AIServiceCoordinator=MagicMock(),
            InteractionHandler=MagicMock(),
            UnifiedAudioProcessor=MagicMock(),
            BotState=MagicMock(),
        ):
            session = GuildSession(guild, bot, ai_service_factories)
            session.bot_state = AsyncMock()
            return session

    @pytest.mark.asyncio
    async def test_handle_finished_recording_creates_background_task(
        self, guild_session_with_mocks
    ):
        """Test that _handle_finished_recording creates a background task."""
        session = guild_session_with_mocks

        audio_data = b"test_audio"

        session._handle_finished_recording(audio_data)

        # Should have created a background task
        assert len(session._background_tasks) == 1

    @pytest.mark.asyncio
    async def test_safe_enter_error_state_with_mismatched_session_id(
        self, guild_session_with_mocks
    ):
        """Test safe enter error state with mismatched session ID."""
        session = guild_session_with_mocks
        session.bot_state.session_id = 123

        await session._safe_enter_error_state(456)  # Different session ID

        # Should not enter error state
        session.bot_state.enter_connection_error_state.assert_not_called()

async def make_floor_session():
    """Use real router, state and coordinator with a deterministic provider."""
    guild = MagicMock(id=123)
    with patch.multiple(
        "src.bot.session.guild_session",
        SessionUIManager=MagicMock(), AudioPlaybackManager=MagicMock(),
        VoiceConnectionManager=MagicMock(), InteractionHandler=MagicMock(),
        UnifiedAudioProcessor=MagicMock(),
    ):
        session = GuildSession(guild, MagicMock(), {})
    session.audio_playback_manager.get_current_playing_response_id.return_value = None
    session._audio_sink = MagicMock()
    session._audio_sink.is_user_input_blocked.return_value = False
    generations = {}
    session._audio_sink.get_user_input_generation.side_effect = lambda user: generations.get(user, 0)
    session._audio_sink.invalidate_user_input.side_effect = lambda user: generations.__setitem__(user, generations.get(user, 0) + 1)
    manager = MagicMock()
    manager.capabilities = ProviderCapabilities(realtime_audio_input=True, server_vad=True, turn_context=True)
    manager.connection_epoch = 1
    manager.is_connected.return_value = True
    manager.observation_context = {}
    manager.processing_audio_format = (16000, 1)
    sequence = []

    async def context(user, _name, **_kwargs):
        sequence.append(("context", user))
        return True

    async def audio(pcm):
        sequence.append(("audio", pcm))
        return True

    async def finalize():
        sequence.append(("finalize",))
        return True

    async def cancel():
        sequence.append(("cancel",))
        return True

    manager.send_turn_context = AsyncMock(side_effect=context)
    manager.send_audio_chunk = AsyncMock(side_effect=audio)
    manager.finalize_input_and_request_response = AsyncMock(side_effect=finalize)
    manager.cancel_ongoing_response = AsyncMock(side_effect=cancel)
    session.ai_coordinator.active_ai_service_manager = manager
    await session.bot_state.set_state(BotStateEnum.STANDBY)
    return session, manager, sequence


@pytest.mark.asyncio
async def test_wake_takeover_preserves_old_tail_before_new_identity_and_audio():
    session, manager, sequence = await make_floor_session()
    first, second = MagicMock(id=1, name="first"), MagicMock(id=2, name="second")
    first.name, second.name = "first", "second"
    await session.on_wake_word_detected(first)
    session._live_audio_buffer.extend(b"old tail")
    session._live_audio_queue.put_nowait((1, "first", b" old queued", 0))
    await session.on_wake_word_detected(second)
    assert session.conversation_router.floor_owner_id == 2
    assert session._live_input_user_id == 2
    assert not session.bot_state.is_active_participant(1)
    assert await session._send_live_provider_chunk(2, "second", b"new voice")
    assert sequence[0] == ("context", 1)
    assert sequence[1][0] == "audio" and sequence[1][1].startswith(b"old tail old queued")
    assert sequence[2:] == [("finalize",), ("cancel",), ("context", 2), ("audio", b"new voice")]
    manager.send_turn_context.assert_has_awaits([call(1, "first", streaming=True), call(2, "second", streaming=True)])


@pytest.mark.asyncio
async def test_previous_owner_ordinary_speech_is_ignored_but_wake_reclaims_floor():
    session, manager, sequence = await make_floor_session()
    first, second = MagicMock(id=1), MagicMock(id=2)
    first.name, second.name = "first", "second"
    await session.on_wake_word_detected(first)
    await session.on_wake_word_detected(second)
    await session._finish_live_audio_input(finalize=False, flush=False)
    await session.bot_state.stop_recording()
    before = len(sequence)
    await session.on_active_speech_detected(first)
    assert session.bot_state.current_state == BotStateEnum.STANDBY
    assert len(sequence) == before
    assert not await session.ai_coordinator.send_audio_stream_chunk(b"unauthorized", 1, "first")
    await session.on_wake_word_detected(first)
    assert session.conversation_router.floor_owner_id == 1
    assert session._live_input_user_id == 1


@pytest.mark.asyncio
async def test_stale_vad_waiting_for_action_lock_cannot_finalize_new_floor_owner():
    session, manager, _ = await make_floor_session()
    first, second = MagicMock(id=1), MagicMock(id=2)
    first.name, second.name = "first", "second"
    await session.on_wake_word_detected(first)
    old_session = session.bot_state.current_session_id
    await session._action_lock.acquire()
    takeover = asyncio.create_task(session.on_wake_word_detected(second))
    await asyncio.sleep(0)
    stale_vad = asyncio.create_task(session.on_vad_speech_end(
        b"old", user_id=1, input_generation=0, session_id=old_session,
    ))
    session._action_lock.release()
    await asyncio.gather(takeover, stale_vad)
    assert session._live_input_active
    assert session._live_input_user_id == 2
    assert session.bot_state.current_state == BotStateEnum.RECORDING
    assert manager.finalize_input_and_request_response.await_count == 1


@pytest.mark.asyncio
async def test_gemini_client_vad_start_uses_short_silence_and_grace():
    session, manager, _ = await make_floor_session()
    manager.capabilities = ProviderCapabilities(
        realtime_audio_input=True, server_vad=False,
        client_vad_streaming=True, turn_context=True,
    )
    session._audio_sink = MagicMock(spec=ManualControlSink)
    session._audio_sink.is_user_input_blocked.return_value = False
    session._audio_sink.get_user_input_generation.return_value = 0
    speaker = MagicMock(id=1)
    speaker.name = "first"

    with patch.object(Config, "GEMINI_LOCAL_VAD_SILENCE_MS", 650):
        await session.on_wake_word_detected(speaker)

    assert session._live_input_active
    assert session._live_input_user_id == 1
    assert session.bot_state.current_state == BotStateEnum.RECORDING
    session._audio_sink.enable_vad.assert_called_once_with(
        True, silence_timeout_ms=650, grace_period_ms=200,
    )
    session._audio_sink.update_session_id.assert_called_once()


@pytest.mark.asyncio
async def test_gemini_short_vad_pause_finalizes_and_owner_continues_without_wake():
    session, manager, sequence = await make_floor_session()
    manager.capabilities = ProviderCapabilities(
        realtime_audio_input=True, server_vad=False,
        client_vad_streaming=True, turn_context=True,
    )
    speaker = MagicMock(id=1)
    speaker.name = "first"
    await session.on_wake_word_detected(speaker)
    previous_session = session.bot_state.current_session_id
    session._live_audio_buffer.extend(b"short utterance tail")

    await session.on_vad_speech_end(
        b"captured utterance", user_id=1, input_generation=0,
        session_id=previous_session,
    )

    manager.finalize_input_and_request_response.assert_awaited_once()
    assert sequence[0] == ("context", 1)
    assert sequence[1][1].startswith(b"short utterance tail")
    assert sequence[2] == ("finalize",)
    assert session.bot_state.current_state == BotStateEnum.STANDBY
    assert not session._live_input_active
    assert session.bot_state.is_active_participant(1)
    assert session.conversation_router.floor_owner_id == 1
    assert session.conversation_router.active_participants == {1}

    await session.on_active_speech_detected(speaker)

    assert session._live_input_active
    assert session._live_input_user_id == 1
    assert session.bot_state.current_state == BotStateEnum.RECORDING
    assert session.bot_state.current_session_id == previous_session + 1
    assert session.conversation_router.floor_owner_id == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("keyword", ["over", "结束"])
async def test_input_end_keyword_finalizes_sent_audio_without_stopping_reply(keyword):
    session, manager, sequence = await make_floor_session()
    speaker = MagicMock(id=1)
    speaker.name = "first"
    await session.on_wake_word_detected(speaker)
    assert await session._send_live_provider_chunk(1, "first", b"already sent question")
    session._live_audio_buffer.extend(b"unsent stop prefix")
    session._live_audio_queue.put_nowait((1, "first", b"unsent queued input", 0))
    session._response_playback_seen = True
    session._audio_sink.is_user_input_blocked.return_value = True
    session._audio_sink.invalidate_user_input(1)

    await session.on_stop_word_detected(speaker, keyword)

    assert sequence == [("context", 1), ("audio", b"already sent question"), ("finalize",)]
    manager.cancel_ongoing_response.assert_not_awaited()
    session.voice_connection.stop_playback.assert_not_called()
    session.audio_playback_manager.interrupt_audio_stream.assert_not_called()
    assert session._response_playback_seen
    assert session._agent_response_pending
    assert not session._live_input_active
    assert not session._live_audio_buffer
    assert session._live_audio_queue.empty()
    assert session.bot_state.current_state == BotStateEnum.STANDBY
    assert not session.bot_state.is_active_participant(1)
    assert session.conversation_router.floor_owner_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword", ["over", "结束"])
async def test_input_end_during_existing_reply_does_not_finalize_or_cancel_again(keyword):
    session, manager, _ = await make_floor_session()
    speaker = MagicMock(id=1)
    speaker.name = "first"
    await session.on_wake_word_detected(speaker)
    assert await session._send_live_provider_chunk(1, "first", b"question")
    await session._finish_live_audio_input(finalize=True, flush=True)
    await session.bot_state.stop_recording()
    session._response_playback_seen = True
    session._audio_sink.is_user_input_blocked.return_value = True

    await session.on_stop_word_detected(speaker, keyword)

    manager.finalize_input_and_request_response.assert_awaited_once()
    manager.cancel_ongoing_response.assert_not_awaited()
    session.voice_connection.stop_playback.assert_not_called()
    assert session._agent_response_pending
    assert session._response_playback_seen


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword", ["over", "结束"])
async def test_non_owner_stop_does_not_touch_current_input_or_reply(keyword):
    session, manager, sequence = await make_floor_session()
    speaker = MagicMock(id=1)
    speaker.name = "first"
    await session.on_wake_word_detected(speaker)
    session._live_audio_buffer.extend(b"owner pending voice")
    session._audio_sink.is_user_input_blocked.side_effect = lambda user: user == 2

    await session.on_stop_word_detected(MagicMock(id=2), keyword)

    assert session._live_input_active
    assert session._live_input_user_id == 1
    assert session._live_audio_buffer == b"owner pending voice"
    assert session.conversation_router.floor_owner_id == 1
    assert session.bot_state.is_active_participant(1)
    assert not sequence
    manager.finalize_input_and_request_response.assert_not_awaited()
    manager.cancel_ongoing_response.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize("owner_present", [True, False])
async def test_shut_up_stops_shared_output_even_without_input_ownership(owner_present):
    session, manager, _ = await make_floor_session()
    if owner_present:
        speaker = MagicMock(id=1)
        speaker.name = "first"
        await session.on_wake_word_detected(speaker)
    session._audio_sink.is_user_input_blocked.return_value = True
    for _ in range(2):
        await session.on_stop_word_detected(MagicMock(id=2), "闭嘴")
    assert manager.cancel_ongoing_response.await_count == 2
    assert session.voice_connection.stop_playback.call_count == 2
    assert session.audio_playback_manager.interrupt_audio_stream.call_count == 2
    if owner_present:
        assert session._live_input_user_id == 1
        assert session.bot_state.is_active_participant(1)


@pytest.mark.asyncio
async def test_server_vad_safety_closure_keeps_floor_until_bilateral_idle_timer():
    session, manager, _ = await make_floor_session()
    speaker = MagicMock(id=1)
    speaker.name = "first"
    await session.on_wake_word_detected(speaker)
    session._live_audio_buffer.extend(b"pending voice")

    await session.on_vad_speech_end(
        b"captured", user_id=1, input_generation=0,
        session_id=session.bot_state.current_session_id,
    )

    manager.finalize_input_and_request_response.assert_awaited_once()
    assert session.bot_state.current_state == BotStateEnum.STANDBY
    assert session.bot_state.is_active_participant(1)
    assert session.conversation_router.floor_owner_id == 1
    assert session._agent_response_pending
    # Pending response/tool work is busy even after ten seconds of user silence.
    assert not session.conversation_router.release_if_idle(busy=True, now=100)
    assert session.conversation_router.floor_owner_id == 1
    assert not session.conversation_router.release_if_idle(busy=False, now=109.9)

    # Once output has finished, the independent monitor owns the idle release.
    session._agent_response_pending = False
    session._response_pending_since = 0
    session.conversation_router.touch(now=0)
    released = asyncio.Event()
    original_clear = session.bot_state.clear_active_participants

    async def clear():
        await original_clear()
        released.set()

    session.bot_state.clear_active_participants = clear
    manager.end_conversation = AsyncMock(return_value=True)
    monitor = asyncio.create_task(session._conversation_monitor_loop())
    try:
        await asyncio.wait_for(released.wait(), 1)
    finally:
        monitor.cancel()
        await monitor
    assert session.conversation_router.floor_owner_id is None
    assert not session.bot_state.is_active_participant(1)
    manager.end_conversation.assert_awaited_once_with(reason="idle")


@pytest.mark.asyncio
async def test_monitor_rechecks_playback_after_waiting_for_handoff_lock():
    session, manager, _ = await make_floor_session()
    manager.capabilities = ProviderCapabilities(
        realtime_audio_input=True, client_vad_streaming=True, turn_context=True,
    )
    speaker = MagicMock(id=1)
    speaker.name = "first"
    await session.on_wake_word_detected(speaker)
    session._finish_live_audio_input = AsyncMock()
    observed, rechecked = asyncio.Event(), asyncio.Event()
    calls = 0

    def playback():
        nonlocal calls
        calls += 1
        if calls == 1:
            observed.set()
            return "old-response"
        rechecked.set()
        return None

    session.audio_playback_manager.get_current_playing_response_id.side_effect = playback
    await session._action_lock.acquire()
    monitor = asyncio.create_task(session._conversation_monitor_loop())
    try:
        await asyncio.wait_for(observed.wait(), 1)
        # The wake handler owns this lock and has replaced the old input and
        # cancelled its playback while the monitor waits on its stale snapshot.
        session._live_stream_revision += 1
        session._live_input_user_id = session._current_turn_user_id = 2
        session._live_audio_buffer.extend(b"new user voice")
        session._action_lock.release()
        await asyncio.wait_for(rechecked.wait(), 1)
        await asyncio.sleep(0)
    finally:
        if session._action_lock.locked():
            session._action_lock.release()
        monitor.cancel()
        await monitor

    session._finish_live_audio_input.assert_not_awaited()
    session._audio_sink.stop_and_get_audio.assert_not_called()
    assert session._live_input_active
    assert session._live_input_user_id == 2
    assert session._live_audio_buffer == b"new user voice"
    assert session.bot_state.current_state == BotStateEnum.RECORDING
    assert not session._response_playback_seen


@pytest.mark.asyncio
async def test_native_vad_keeps_owner_audio_open_through_reply_and_next_question():
    session, manager, sequence = await make_floor_session()
    speaker = MagicMock(id=1)
    speaker.name = "first"
    await session.on_wake_word_detected(speaker)
    assert await session._send_live_provider_chunk(1, "first", b"first question")
    revision = session._live_stream_revision
    seen = asyncio.Event()
    calls = 0

    def playback():
        nonlocal calls
        calls += 1
        if calls >= 2:
            seen.set()
        return "agent-answer"

    session.audio_playback_manager.get_current_playing_response_id.side_effect = playback
    monitor = asyncio.create_task(session._conversation_monitor_loop())
    try:
        await asyncio.wait_for(seen.wait(), 1)
        assert session._live_input_active
        assert session.bot_state.current_state == BotStateEnum.RECORDING
        assert session._live_stream_revision == revision
        assert await session._send_live_provider_chunk(1, "first", b"second question onset")
        assert not await session._send_live_provider_chunk(2, "other", b"unadmitted speech")
    finally:
        monitor.cancel()
        await monitor

    assert sequence == [("context", 1), ("audio", b"first question"),
                        ("audio", b"second question onset")]
    manager.finalize_input_and_request_response.assert_not_awaited()
    manager.cancel_ongoing_response.assert_not_awaited()
    session._audio_sink.stop_and_get_audio.assert_not_called()

@pytest.mark.asyncio
@pytest.mark.parametrize("next_speaker_id", [1, 2])
async def test_context_failure_retires_recording_and_next_wake_can_send(next_speaker_id):
    session, manager, sequence = await make_floor_session()
    first = MagicMock(id=1)
    first.name = "first"
    await session.on_wake_word_detected(first)
    old_session_id = session.bot_state.current_session_id
    manager.send_turn_context.side_effect = None
    manager.send_turn_context.return_value = False
    session._live_audio_queue.put_nowait((1, "first", b"x" * 6400, 0))
    session._live_audio_queue.put_nowait((1, "first", b"old queued", 0))
    recovered = asyncio.Event()
    original_recover = session._recover_failed_live_input

    async def recover(*args):
        await original_recover(*args)
        recovered.set()

    session._recover_failed_live_input = recover
    task = asyncio.create_task(session._live_audio_stream_loop())
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
    finally:
        task.cancel()
        await task

    assert session.bot_state.current_state == BotStateEnum.STANDBY
    assert not session._live_input_active
    assert session._live_input_user_id is None
    assert session._current_turn_user_id is None
    assert not session._live_audio_buffer
    assert session._live_audio_queue.empty()
    assert session.conversation_router.floor_owner_id == 1
    assert session.bot_state.is_active_participant(1)
    assert session._user_input_generation(1) == 1
    manager.send_audio_chunk.assert_not_awaited()
    manager.finalize_input_and_request_response.assert_not_awaited()
    manager.cancel_ongoing_response.assert_not_awaited()

    # The old sink recording cannot fall back to buffered upload after failure.
    await session.on_vad_speech_end(
        b"old captured recording", user_id=1, input_generation=0,
        session_id=old_session_id,
    )
    manager.send_turn_context.assert_awaited_once()
    manager.send_audio_chunk.assert_not_awaited()

    manager.send_turn_context.return_value = True
    next_speaker = MagicMock(id=next_speaker_id)
    next_speaker.name = "next"
    await session.on_wake_word_detected(next_speaker)
    assert session.bot_state.current_state == BotStateEnum.RECORDING
    assert session._live_input_user_id == next_speaker_id
    assert session.conversation_router.floor_owner_id == next_speaker_id
    assert await session._send_live_provider_chunk(next_speaker_id, "next", b"new speech")
    manager.send_audio_chunk.assert_awaited_once_with(b"new speech")
    assert manager.send_turn_context.await_args.args[0] == next_speaker_id


@pytest.mark.asyncio
async def test_failed_stream_recovery_waiting_for_lock_cannot_retire_new_owner():
    session, manager, _ = await make_floor_session()
    first, second = MagicMock(id=1), MagicMock(id=2)
    first.name, second.name = "first", "second"
    await session.on_wake_word_detected(first)
    old_revision = session._live_stream_revision
    await session._action_lock.acquire()
    takeover = asyncio.create_task(session.on_wake_word_detected(second))
    await asyncio.sleep(0)
    recovery = asyncio.create_task(session._recover_failed_live_input(1, old_revision, 0))
    await asyncio.sleep(0)
    session._action_lock.release()
    await asyncio.gather(takeover, recovery)

    assert session._live_input_active
    assert session._live_input_user_id == 2
    assert session._current_turn_user_id == 2
    assert session.bot_state.current_state == BotStateEnum.RECORDING
    assert session.conversation_router.floor_owner_id == 2
    assert session._user_input_generation(2) == 0
    assert await session._send_live_provider_chunk(2, "second", b"new owner audio")
    manager.send_audio_chunk.assert_awaited_once_with(b"new owner audio")
