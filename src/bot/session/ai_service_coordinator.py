"""
AI Service Coordinator module.

This module defines the AIServiceCoordinator class, which is responsible for
managing the lifecycle and interactions with the AI service providers.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, Optional, Tuple

from discord.ext import commands
import discord

from src.ai_services.interface import IRealtimeAIServiceManager
from src.ai_services.web_search import discord_search_message
from src.audio.playback import AudioPlaybackManager
from src.config.config import Config
from src.bot.state import BotState
from src.observability import get_observer
from src.utils.logger import get_logger

logger = get_logger(__name__)


class AIServiceCoordinator:
    """
    Manages the lifecycle and interactions with AI service providers.
    """

    def __init__(
        self,
        bot_state: BotState,
        audio_playback_manager: AudioPlaybackManager,
        ai_service_factories: Dict[str, tuple],
        guild_id: int,
    ):
        self.bot_state = bot_state
        self.audio_playback_manager = audio_playback_manager
        self.ai_service_factories = ai_service_factories
        self.guild_id = guild_id
        self.active_ai_service_manager: Optional[IRealtimeAIServiceManager] = None
        self._last_speaker_id: Optional[int] = None
        self._stream_context_key = None
        self._input_lock = asyncio.Lock()
        self.is_user_input_blocked: Callable[[int], bool] = lambda _user_id: False
        self.get_user_input_generation: Callable[[int], int] = lambda _user_id: 0

    def observe_input_event(self, event: str, user_id=None, **fields) -> None:
        observer = get_observer()
        manager = self.active_ai_service_manager
        context = getattr(manager, "observation_context", {})
        if not isinstance(context, dict):
            context = {}
        observer.emit(
            event, **{**context, **fields,
                      "guild_id": observer.pseudonym(self.guild_id),
                      "speaker_id": observer.pseudonym(user_id) if user_id is not None else None},
        )

    def _context_key(self, manager, user_id: int):
        return (
            id(manager), user_id, self.bot_state.current_session_id,
            getattr(manager, "connection_epoch", 0),
            self.get_user_input_generation(user_id),
        )

    def _input_is_current(self, manager, user_id: int, context_key) -> bool:
        """Recheck admission and transport after every awaited metadata write."""
        return bool(
            manager is self.active_ai_service_manager
            and manager.is_connected()
            and self._context_key(manager, user_id) == context_key
            and self.bot_state.is_active_participant(user_id)
            and not self.is_user_input_blocked(user_id)
        )

    async def _prepare_context(self, manager, user_id, display_name, *, streaming):
        """Order context before audio on one connection; success is a wire send.

        Realtime events on the same WebSocket preserve order. This does not
        claim a remote conversation-item acknowledgement or model completion.
        """
        key = self._context_key(manager, user_id)
        if not self._input_is_current(manager, user_id, key):
            return False
        needs_context = key != self._stream_context_key or (
            manager.capabilities.turn_context and not streaming
        )
        if needs_context:
            self._stream_context_key = None
            if manager.capabilities.turn_context:
                accepted = await manager.send_turn_context(
                    user_id, display_name, streaming=streaming
                )
            else:
                accepted = await manager.send_speaker_marker(user_id, display_name)
            if not accepted or not self._input_is_current(manager, user_id, key):
                self.observe_input_event(
                    "input.context.rejected", user_id, status="blocked",
                    reason="send_failed" if not accepted else "input_changed",
                )
                return False
            self._stream_context_key = key
            self._last_speaker_id = user_id
            self.observe_input_event(
                "input.context.ready", user_id, status="sent", streaming=streaming,
                reason="ordered_transport_send",
            )
        return True

    def is_connected(self) -> bool:
        """Checks if the active AI service manager is connected."""
        return (
            self.active_ai_service_manager is not None
            and self.active_ai_service_manager.is_connected()
        )

    def get_processing_audio_format(self) -> Optional[Tuple[int, int]]:
        """Gets the audio format required by the current AI service for processing."""
        if self.active_ai_service_manager:
            return self.active_ai_service_manager.processing_audio_format
        return None

    def supports_server_vad_streaming(self) -> bool:
        manager = self.active_ai_service_manager
        return bool(
            manager
            and manager.capabilities.realtime_audio_input
            and manager.capabilities.server_vad
        )

    def supports_client_vad_streaming(self) -> bool:
        """Stream immediately while local VAD controls explicit turn boundaries."""
        manager = self.active_ai_service_manager
        return bool(manager and manager.capabilities.realtime_audio_input
                    and manager.capabilities.client_vad_streaming is True)

    async def send_audio_stream_chunk(
        self, pcm_data: bytes, user_id: int, display_name: str
    ) -> bool:
        """Keep metadata and its following audio adjacent across callers."""
        generation = self.get_user_input_generation(user_id)
        async with self._input_lock:
            if generation != self.get_user_input_generation(user_id):
                return False
            return await self._send_audio_stream_chunk(pcm_data, user_id, display_name)

    async def _send_audio_stream_chunk(
        self,
        pcm_data: bytes,
        user_id: int,
        display_name: str,
    ) -> bool:
        """Send one already-paced PCM chunk without finalizing the turn."""
        manager = self.active_ai_service_manager
        if not self.is_connected() or not manager:
            return False
        if not self.bot_state.is_active_participant(user_id) or self.is_user_input_blocked(user_id):
            self.observe_input_event("input.audio.rejected", user_id, reason="admission_closed")
            logger.warning(
                "Streaming upload gate rejected inactive user %s in guild %s.",
                user_id,
                self.guild_id,
            )
            return False
        if not await self._prepare_context(
            manager, user_id, display_name, streaming=True
        ):
            return False
        return await manager.send_audio_chunk(pcm_data)

    async def finalize_audio_stream(self) -> bool:
        """Flush a live audio stream and request a provider response."""
        async with self._input_lock:
            manager = self.active_ai_service_manager
            if not manager or not manager.is_connected():
                return False
            key = self._stream_context_key
            self._stream_context_key = None
            if key is not None and not self._input_is_current(manager, key[1], key):
                return False
            return await manager.finalize_input_and_request_response()

    async def finish_stopped_user_input(self, user_id: int) -> bool:
        """End already-sent input after over/结束, without uploading or cancelling."""
        async with self._input_lock:
            manager = self.active_ai_service_manager
            key = self._stream_context_key
            if (not manager or not manager.is_connected() or key is None
                    or key[:4] != self._context_key(manager, user_id)[:4]
                    or not self.is_user_input_blocked(user_id)):
                return False
            self._stream_context_key = None
            return await manager.finalize_input_and_request_response()

    async def end_conversation(self, *, reason: str) -> bool:
        import inspect
        async with self._input_lock:
            manager = self.active_ai_service_manager
            method = getattr(manager, "end_conversation", None)
            if method and inspect.iscoroutinefunction(method):
                self._stream_context_key = None
                return await method(reason=reason)
            return False

    async def cancel_ongoing_response(self) -> bool:
        """Cancels any ongoing response from the AI service."""
        async with self._input_lock:
            manager = self.active_ai_service_manager
            self._stream_context_key = None
            if manager and manager.is_connected():
                return await manager.cancel_ongoing_response()
            return False

    async def send_audio_turn(
        self,
        pcm_data: bytes,
        user_id: Optional[int] = None,
        display_name: Optional[str] = None,
    ) -> bool:
        """Serialize complete buffered turns into the guild's single session."""
        generation = self.get_user_input_generation(user_id)
        async with self._input_lock:
            if generation != self.get_user_input_generation(user_id):
                return False
            return await self._send_audio_turn(pcm_data, user_id, display_name)

    async def _send_audio_turn(
        self,
        pcm_data: bytes,
        user_id: Optional[int] = None,
        display_name: Optional[str] = None,
    ) -> bool:
        """Sends a full audio turn (chunk + finalize) to the AI service."""
        # AI connection state TOCTOU fix: Capture manager atomically to prevent race
        manager = self.active_ai_service_manager
        if not self.is_connected() or not manager:
            logger.error(f"Cannot send audio for guild {self.guild_id}: Not connected.")
            return False

        if user_id is None:
            self.observe_input_event("input.audio.rejected", reason="missing_identity")
            logger.warning("Upload gate rejected audio without a speaker identity.")
            return False
        if not self.bot_state.is_active_participant(user_id) or self.is_user_input_blocked(user_id):
            self.observe_input_event("input.audio.rejected", user_id, reason="admission_closed")
            logger.warning(
                "Upload gate rejected audio for inactive user %s in guild %s.",
                user_id,
                self.guild_id,
            )
            return False

        if not await self._prepare_context(
            manager, user_id, display_name or str(user_id), streaming=False
        ):
            return False
        context_key = self._stream_context_key

        if not await manager.send_audio_chunk(pcm_data):
            logger.error(
                f"Failed to send audio chunk to AI service for guild {self.guild_id}."
            )
            return False

        if user_id is not None:
            self._last_speaker_id = user_id

        if not self._input_is_current(manager, user_id, context_key):
            return False
        if not await manager.finalize_input_and_request_response():
            logger.error(
                f"Failed to finalize input for AI service for guild {self.guild_id}."
            )
            return False

        logger.info(
            f"Successfully sent audio and requested response from AI service for guild {self.guild_id}."
        )
        return True

    async def shutdown(self) -> None:
        """Shuts down the connection to the current AI provider."""
        async with self._input_lock:
            await self._shutdown()

    async def _shutdown(self) -> None:
        """Detach before disconnecting so subsequent input cannot use this manager."""
        # AI connection state TOCTOU fix: Capture manager atomically
        manager = self.active_ai_service_manager
        self.active_ai_service_manager = None
        self._last_speaker_id = None
        self._stream_context_key = None
        if manager:
            provider_name = self.bot_state.active_ai_provider_name
            logger.info(
                f"Shutting down AI provider '{provider_name}' for guild {self.guild_id}"
            )
            await manager.cancel_ongoing_response()
            await manager.disconnect()
            logger.info(f"Disconnected from {provider_name} for guild {self.guild_id}.")
        self.active_ai_service_manager = None
        self._last_speaker_id = None
        self._stream_context_key = None

    async def ensure_connected(
        self,
        ctx: commands.Context,
        on_connect: Callable[[], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]],
    ) -> bool:
        """Ensures the AI service is initialized and connected, creating it if necessary."""
        if not self.is_connected():
            if not self.active_ai_service_manager:
                default_provider = Config.AI_SERVICE_PROVIDER
                if default_provider not in self.ai_service_factories:
                    if not self.ai_service_factories:
                        await ctx.send("No AI providers are configured.")
                        return False
                    fallback_provider = next(iter(self.ai_service_factories))
                    logger.warning(
                        "Configured provider '%s' is unavailable for guild %s; "
                        "falling back to '%s'.",
                        default_provider,
                        self.guild_id,
                        fallback_provider,
                    )
                    default_provider = fallback_provider
                if not await self._create_and_set_manager(default_provider, ctx):
                    return False

            if not await self.active_ai_service_manager.connect(
                on_connect=on_connect, on_disconnect=on_disconnect
            ):
                await ctx.send(
                    "Failed to connect to the AI service. Please try again later."
                )
                self.active_ai_service_manager = None
                return False
        return True

    async def switch_provider(
        self,
        provider_name: str,
        ctx: commands.Context,
        is_voice_connected: bool,
        on_connect: Callable[[], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]],
    ) -> bool:
        """Handles the logic of switching the AI provider."""
        if (
            self.bot_state.active_ai_provider_name == provider_name
            and self.is_connected()
        ):
            await ctx.send(f"AI provider is already set to '{provider_name.upper()}'.")
            return True

        new_manager = await self._validate_new_provider(
            provider_name, ctx, is_voice_connected, on_connect, on_disconnect
        )
        if not new_manager:
            return False

        await self.shutdown()
        self.active_ai_service_manager = new_manager
        await self.bot_state.set_active_ai_provider_name(provider_name)
        return True

    async def _create_and_set_manager(
        self, provider_name: str, ctx: commands.Context
    ) -> bool:
        """Creates and sets the AI service manager instance."""
        manager_class, service_config = self.ai_service_factories[provider_name]
        try:
            manager_instance = manager_class(
                audio_playback_manager=self.audio_playback_manager,
                service_config=self._config_for_channel(service_config, ctx),
            )
            self.active_ai_service_manager = manager_instance
            await self.bot_state.set_active_ai_provider_name(provider_name)
            logger.info(
                f"Successfully created AI manager for '{provider_name}' in guild {self.guild_id}."
            )
            return True
        except (ValueError, Exception) as e:
            logger.error(
                f"Failed to create AI manager for '{provider_name}' in guild {self.guild_id}: {e}",
                exc_info=True,
            )
            await ctx.send(
                f"Failed to initialize AI provider '{provider_name.upper()}'. "
                f"Check configuration (e.g., API key). Error: {e}"
            )
            self.active_ai_service_manager = None
            return False

    async def _validate_new_provider(
        self,
        provider_name: str,
        ctx: commands.Context,
        is_voice_connected: bool,
        on_connect: Callable[[], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]],
    ) -> Optional[IRealtimeAIServiceManager]:
        """Validates a new provider by attempting to create and connect it."""
        manager_class, service_config = self.ai_service_factories[provider_name]
        try:
            new_manager = manager_class(
                audio_playback_manager=self.audio_playback_manager,
                service_config=self._config_for_channel(service_config, ctx),
            )
        except (ValueError, Exception) as e:
            logger.error(
                f"Failed to create AI manager for '{provider_name}' in guild {self.guild_id}: {e}",
                exc_info=True,
            )
            await ctx.send(
                f"Failed to initialize AI provider '{provider_name.upper()}'. "
                f"Check configuration. Error: {e}"
            )
            return None

        if is_voice_connected:
            if not await new_manager.connect(
                on_connect=on_connect, on_disconnect=on_disconnect
            ):
                await ctx.send(
                    f"Failed to connect to '{provider_name.upper()}'. Switch aborted."
                )
                await new_manager.disconnect()
                return None
        return new_manager

    @staticmethod
    def _config_for_channel(service_config: dict, ctx) -> dict:
        """Bind search citations to the session's text channel, without mutating defaults."""
        async def publish(result: dict) -> None:
            await ctx.send(
                discord_search_message(result),
                allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True,
            )

        return {**service_config, "on_web_search_result": publish}
