import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.session.ai_service_coordinator import AIServiceCoordinator
from src.ai_services.interface import ProviderCapabilities


@pytest.fixture
def coordinator():
    bot_state = MagicMock()
    bot_state.is_active_participant.side_effect = lambda user_id: user_id == 1
    manager = MagicMock()
    manager.is_connected.return_value = True
    manager.send_speaker_marker = AsyncMock(return_value=True)
    manager.send_audio_chunk = AsyncMock(return_value=True)
    manager.finalize_input_and_request_response = AsyncMock(return_value=True)
    manager.capabilities = ProviderCapabilities(
        realtime_audio_input=True, server_vad=True
    )
    manager.connection_epoch = 1
    bot_state.current_session_id = 1
    result = AIServiceCoordinator(bot_state, MagicMock(), {}, guild_id=42)
    result.active_ai_service_manager = manager
    return result, manager


@pytest.mark.asyncio
async def test_upload_gate_rejects_non_participant_audio(coordinator):
    result, manager = coordinator

    assert await result.send_audio_turn(b"private", user_id=2) is False
    manager.send_audio_chunk.assert_not_awaited()


@pytest.mark.asyncio
async def test_speaker_marker_only_emitted_on_actual_switch(coordinator):
    result, manager = coordinator

    assert await result.send_audio_turn(b"one", user_id=1, display_name="Alice")
    assert await result.send_audio_turn(b"two", user_id=1, display_name="Alice")

    manager.send_speaker_marker.assert_awaited_once_with(1, "Alice")
    assert manager.send_audio_chunk.await_count == 2


@pytest.mark.asyncio
async def test_realtime_stream_chunk_does_not_finalize(coordinator):
    result, manager = coordinator

    assert await result.send_audio_stream_chunk(b"pcm", 1, "Alice")

    manager.send_audio_chunk.assert_awaited_once_with(b"pcm")
    manager.finalize_input_and_request_response.assert_not_awaited()
    assert result.supports_server_vad_streaming() is True


@pytest.mark.asyncio
async def test_realtime_stream_can_be_force_finalized(coordinator):
    result, manager = coordinator

    assert await result.finalize_audio_stream()

    manager.finalize_input_and_request_response.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_failed_legacy_identity_blocks_every_audio_path(coordinator, streaming):
    result, manager = coordinator
    manager.send_speaker_marker.return_value = False
    send = result.send_audio_stream_chunk if streaming else result.send_audio_turn
    assert not await send(b"private", 1, "Alice")
    manager.send_audio_chunk.assert_not_awaited()
    manager.finalize_input_and_request_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_identity_blocks_audio(coordinator):
    result, manager = coordinator
    assert not await result.send_audio_turn(b"private")
    manager.send_audio_chunk.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_await_completes_before_audio_and_next_speaker(coordinator):
    result, manager = coordinator
    result.bot_state.is_active_participant.side_effect = lambda _user: True
    manager.capabilities = ProviderCapabilities(turn_context=True)
    started, release = asyncio.Event(), asyncio.Event()
    sequence = []

    async def context(user_id, _name, **_kwargs):
        sequence.append(("context_start", user_id))
        if user_id == 1:
            started.set()
            await release.wait()
        sequence.append(("context_done", user_id))
        return True

    async def audio(data):
        sequence.append(("audio", data))
        return True

    manager.send_turn_context = AsyncMock(side_effect=context)
    manager.send_audio_chunk.side_effect = audio
    first = asyncio.create_task(result.send_audio_stream_chunk(b"one", 1, "Alice"))
    await started.wait()
    second = asyncio.create_task(result.send_audio_stream_chunk(b"two", 2, "Bob"))
    await asyncio.sleep(0)
    manager.send_audio_chunk.assert_not_awaited()
    assert sequence == [("context_start", 1)]
    release.set()
    assert all(await asyncio.gather(first, second))
    assert sequence == [
        ("context_start", 1), ("context_done", 1), ("audio", b"one"),
        ("context_start", 2), ("context_done", 2), ("audio", b"two"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["epoch", "session", "manager", "admission", "stop"])
async def test_changed_input_during_context_is_discarded(coordinator, change):
    result, manager = coordinator

    async def context(*_args):
        if change == "epoch":
            manager.connection_epoch += 1
        elif change == "session":
            result.bot_state.current_session_id += 1
        elif change == "manager":
            result.active_ai_service_manager = MagicMock()
        elif change == "admission":
            result.bot_state.is_active_participant.side_effect = lambda _user: False
        else:
            result.is_user_input_blocked = lambda _user: True
        return True

    manager.send_speaker_marker.side_effect = context
    assert not await result.send_audio_stream_chunk(b"private", 1, "Alice")
    manager.send_audio_chunk.assert_not_awaited()
    assert result._stream_context_key is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["epoch", "session", "manager"])
async def test_context_is_repeated_after_reconnect_or_session_change(coordinator, change):
    result, manager = coordinator
    assert await result.send_audio_stream_chunk(b"before", 1, "Alice")
    if change == "epoch":
        manager.connection_epoch += 1
    elif change == "session":
        result.bot_state.current_session_id += 1
    else:
        replacement = MagicMock()
        replacement.is_connected.return_value = True
        replacement.connection_epoch = manager.connection_epoch
        replacement.capabilities = manager.capabilities
        replacement.send_speaker_marker = manager.send_speaker_marker
        replacement.send_audio_chunk = AsyncMock(return_value=True)
        result.active_ai_service_manager = replacement
    assert await result.send_audio_stream_chunk(b"after", 1, "Alice")
    assert manager.send_speaker_marker.await_count == 2


@pytest.mark.asyncio
async def test_finalize_waits_for_audio_and_rechecks_stop_gate(coordinator):
    result, manager = coordinator
    started, release = asyncio.Event(), asyncio.Event()

    async def audio(_data):
        started.set()
        await release.wait()
        return True

    manager.send_audio_chunk.side_effect = audio
    sending = asyncio.create_task(result.send_audio_stream_chunk(b"pcm", 1, "Alice"))
    await started.wait()
    finalizing = asyncio.create_task(result.finalize_audio_stream())
    await asyncio.sleep(0)
    manager.finalize_input_and_request_response.assert_not_awaited()
    result.is_user_input_blocked = lambda _user: True
    release.set()
    assert await sending
    assert not await finalizing
    manager.finalize_input_and_request_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_then_wake_drops_audio_already_waiting_for_input_lock(coordinator):
    result, manager = coordinator
    generation = 1
    result.get_user_input_generation = lambda _user: generation
    await result._input_lock.acquire()
    sending = asyncio.create_task(result.send_audio_stream_chunk(b"old", 1, "Alice"))
    await asyncio.sleep(0)
    generation = 3
    result._input_lock.release()
    assert not await sending
    manager.send_audio_chunk.assert_not_awaited()
    assert await result.send_audio_stream_chunk(b"new", 1, "Alice")
    manager.send_audio_chunk.assert_awaited_once_with(b"new")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "speaker", "epoch", "session", "unblocked"])
async def test_over_finalizes_only_matching_closed_input_without_cancel(coordinator, change):
    result, manager = coordinator
    assert await result.send_audio_stream_chunk(b"question", 1, "Alice")
    result.is_user_input_blocked = lambda _user: change != "unblocked"
    result.get_user_input_generation = lambda _user: 1
    result.bot_state.is_active_participant.side_effect = lambda _user: False
    if change == "epoch":
        manager.connection_epoch += 1
    elif change == "session":
        result.bot_state.current_session_id += 1
    accepted = await result.finish_stopped_user_input(2 if change == "speaker" else 1)
    assert bool(accepted) == (change is None)
    assert manager.finalize_input_and_request_response.await_count == (1 if change is None else 0)
    manager.cancel_ongoing_response.assert_not_called()
    manager.send_audio_chunk.assert_awaited_once_with(b"question")
