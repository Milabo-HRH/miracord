from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ai_services.providers.gemini.manager import GeminiRealtimeManager
from src.observability import JsonlObserver
from src.ai_services.providers.gemini.event_handler import TurnStartEvent, TurnEndEvent
import asyncio


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="Known gap: cancellation suppresses the current response ID, not a later unsolicited response after its boundary.")
async def test_cancel_keeps_later_unsolicited_response_silent(manager):
    manager._audio_playback_manager.start_audio_stream = AsyncMock()
    manager._audio_playback_manager.add_audio_chunk = AsyncMock()
    use_server_vad(manager)
    assert await manager.send_turn_context(42, "Alice")
    assert await manager.send_audio_chunk(b"question")
    audio = {"model_turn": {"parts": [{"inline_data": {"mime_type": "audio/pcm", "data": b"reply"}}]}}
    await manager._dispatch_message({"server_content": audio})
    manager._audio_playback_manager.add_audio_chunk.assert_awaited_once()
    await manager.cancel_ongoing_response()
    await manager._dispatch_message({"server_content": {"turn_complete": True}})
    manager._audio_playback_manager.add_audio_chunk.reset_mock()
    # No fresh context or PCM was submitted after cancellation.
    await manager._dispatch_message({"server_content": audio})
    manager._audio_playback_manager.add_audio_chunk.assert_not_called()


@pytest.fixture
def manager(tmp_path):
    observer = JsonlObserver(tmp_path / "trace.jsonl", enabled=False)
    playback = MagicMock()
    playback.end_audio_stream = AsyncMock()
    playback.interrupt_playback = AsyncMock()
    instance = GeminiRealtimeManager(playback, {
        "api_key": "offline-key", "model_name": "gemini-3.1-flash-live-preview",
        "live_connect_config": {"realtime_input_config": {"automatic_activity_detection": {"disabled": True}}},
        "processing_audio_frame_rate": 16000, "processing_audio_channels": 1,
        "response_audio_frame_rate": 24000, "response_audio_channels": 1,
        "observer": observer, "gemini_client": MagicMock(),
    })
    session = MagicMock()
    session.send_realtime_input = AsyncMock()
    instance._get_active_session = AsyncMock(return_value=session)
    instance._connection_handler_inst.disconnect = AsyncMock()
    return instance


@pytest.mark.asyncio
async def test_audio_uses_current_gemini_live_audio_field(manager) -> None:
    session = await manager._get_active_session()
    assert await manager.send_turn_context(42, "Alice")
    session.send_realtime_input.reset_mock()

    assert await manager.send_audio_chunk(b"pcm") is True

    session.send_realtime_input.assert_awaited_once()
    kwargs = session.send_realtime_input.await_args.kwargs
    assert "audio" in kwargs
    assert "media" not in kwargs
    assert kwargs["audio"].data == b"pcm"
    assert kwargs["audio"].mime_type == "audio/pcm;rate=16000"


@pytest.mark.asyncio
async def test_audio_is_paced_in_100ms_chunks(manager) -> None:
    session = await manager._get_active_session()
    assert await manager.send_turn_context(42, "Alice")
    session.send_realtime_input.reset_mock()

    with pytest.MonkeyPatch.context() as monkeypatch:
        sleep = AsyncMock()
        monkeypatch.setattr(
            "src.ai_services.providers.gemini.manager.asyncio.sleep", sleep
        )
        assert await manager.send_audio_chunk(b"x" * 6400) is True

    assert session.send_realtime_input.await_count == 2
    sleep.assert_awaited_once_with(0.1)


@pytest.mark.asyncio
async def test_context_precedes_audio_inside_one_manual_activity(manager):
    session = await manager._get_active_session()
    assert not await manager.send_audio_chunk(b"pcm")
    assert await manager.send_turn_context(42, "Alice", streaming=True)
    assert await manager.send_audio_chunk(b"pcm")
    assert await manager.finalize_input_and_request_response()
    assert await manager.finalize_input_and_request_response()
    calls = session.send_realtime_input.await_args_list
    assert [next(iter(call.kwargs)) for call in calls] == ["activity_start", "text", "audio", "activity_end"]
    assert '"discordUserId":"42"' in calls[1].kwargs["text"]
    assert manager.capabilities.turn_context and manager.capabilities.client_vad_streaming
    assert not manager.capabilities.server_vad and not manager.capabilities.cancel_response


@pytest.mark.asyncio
async def test_context_failure_never_sends_audio_or_empty_activity_end(manager):
    session = await manager._get_active_session()
    session.send_realtime_input.side_effect = [None, RuntimeError("private-key")]
    assert not await manager.send_turn_context(42, "Alice")
    assert not await manager.send_audio_chunk(b"pcm")
    assert [next(iter(call.kwargs)) for call in session.send_realtime_input.await_args_list] == ["activity_start", "text"]
    manager._connection_handler_inst.disconnect.assert_awaited_once()
    assert manager._needs_reconnect


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_empty_activity_is_abandoned_without_invalid_end(manager, cancel):
    session = await manager._get_active_session()
    assert await manager.send_turn_context(42, "Alice")
    if cancel:
        assert await manager.cancel_ongoing_response()
    else:
        assert await manager.finalize_input_and_request_response()
    assert [next(iter(call.kwargs)) for call in session.send_realtime_input.await_args_list] == ["activity_start", "text"]
    manager._connection_handler_inst.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_text_replay_sends_one_combined_text_without_audio_activity(manager):
    session = await manager._get_active_session()
    assert await manager.send_text_turn("Question", context_text="Reviewed context")
    session.send_realtime_input.assert_awaited_once()
    assert session.send_realtime_input.await_args.kwargs == {"text": "Reviewed context\n\nCurrent user question:\nQuestion"}


def use_server_vad(manager):
    manager._session_config["realtime_input_config"]["automatic_activity_detection"] = {"disabled": False}


@pytest.mark.asyncio
async def test_server_vad_prepares_context_then_streams_until_explicit_close(manager):
    use_server_vad(manager)
    session = await manager._get_active_session()

    async def acknowledge(**kwargs):
        if "text" in kwargs:
            await manager._dispatch_message({"server_content": {"turn_complete": True}})

    session.send_realtime_input.side_effect = acknowledge
    assert await manager.send_turn_context(42, "Alice", streaming=True)
    assert await manager.send_audio_chunk(b"pcm")
    await manager._dispatch_message({"server_content": {"output_transcription": {"text": "Answer"}, "turn_complete": True}})
    assert manager._context_prepared and manager._activity_open
    assert await manager.send_audio_chunk(b"more pcm")
    kinds = [next(iter(call.kwargs)) for call in session.send_realtime_input.await_args_list]
    assert kinds == ["text", "audio", "audio"]
    assert await manager.finalize_input_and_request_response()
    assert await manager.finalize_input_and_request_response()
    kinds = [next(iter(call.kwargs)) for call in session.send_realtime_input.await_args_list]
    assert kinds == ["text", "audio", "audio", "audio_stream_end"]
    assert manager.capabilities.server_vad and not manager.capabilities.client_vad_streaming


@pytest.mark.asyncio
async def test_context_transport_write_blocks_audio_until_done_without_model_ack(manager):
    use_server_vad(manager)
    session = await manager._get_active_session()
    release = asyncio.Event()
    session.send_realtime_input.side_effect = lambda **kwargs: None

    async def blocked_send(**kwargs):
        await release.wait()

    session.send_realtime_input.side_effect = blocked_send
    pending = asyncio.create_task(manager.send_turn_context(42, "Alice"))
    await asyncio.sleep(0)
    assert not pending.done()
    assert not await manager.send_audio_chunk(b"must wait")
    release.set()
    assert await pending
    assert manager._context_prepared and manager._context_transport_ready


@pytest.mark.asyncio
async def test_metadata_output_before_first_pcm_stays_suppressed_after_pcm(manager):
    use_server_vad(manager)
    assert await manager.send_turn_context(42, "Alice")
    await manager._dispatch_message({"server_content": {"model_turn": {"parts": [{"inline_data": {"mime_type": "audio/pcm", "data": b"metadata audio"}}]}}})
    metadata_id = manager._response_id
    manager._audio_playback_manager.add_audio_chunk.assert_not_called()
    assert await manager.send_audio_chunk(b"question")
    assert not manager._context_update_pending
    assert metadata_id in manager._suppressed_responses
    await manager._dispatch_message({"server_content": {"model_turn": {"parts": [{"inline_data": {"mime_type": "audio/pcm", "data": b"late metadata"}}]}, "turn_complete": True}})
    manager._audio_playback_manager.add_audio_chunk.assert_not_called()
    assert manager._context_prepared


@pytest.mark.asyncio
async def test_prior_response_end_cannot_complete_suspended_context_transport(manager):
    use_server_vad(manager)
    await manager._dispatch_event(TurnStartEvent("old_reply"))
    session = await manager._get_active_session()
    release = asyncio.Event()

    async def blocked_send(**kwargs):
        if "text" in kwargs:
            await release.wait()

    session.send_realtime_input.side_effect = blocked_send
    pending = asyncio.create_task(manager.send_turn_context(42, "Alice"))
    await asyncio.sleep(0)
    await manager._dispatch_event(TurnEndEvent("old_reply"))
    assert not pending.done() and not manager._context_transport_ready
    release.set()
    assert await pending


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["epoch", "cancel", "loss"])
async def test_context_transport_change_during_write_never_opens_audio(manager, change):
    use_server_vad(manager)
    session = await manager._get_active_session()
    release = asyncio.Event()

    async def blocked_send(**kwargs):
        await release.wait()

    session.send_realtime_input.side_effect = blocked_send
    pending = asyncio.create_task(manager.send_turn_context(42, "Alice"))
    await asyncio.sleep(0)
    if change == "epoch":
        manager.connection_epoch += 1
    elif change == "cancel":
        await manager.cancel_ongoing_response()
    else:
        await manager._transport_lost()
    release.set()
    assert not await pending
    assert not await manager.send_audio_chunk(b"blocked")
    assert not manager._context_transport_ready


@pytest.mark.asyncio
async def test_server_context_send_failure_fails_closed(manager):
    use_server_vad(manager)
    session = await manager._get_active_session()
    session.send_realtime_input.side_effect = RuntimeError("failed send")
    assert not await manager.send_turn_context(42, "Alice")
    assert not await manager.send_audio_chunk(b"blocked")
    manager._connection_handler.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_during_first_pcm_write_cannot_reopen_input(manager):
    use_server_vad(manager)
    assert await manager.send_turn_context(42, "Alice")
    session = await manager._get_active_session()
    release = asyncio.Event()

    async def blocked_send(**kwargs):
        if "audio" in kwargs:
            await release.wait()

    session.send_realtime_input.side_effect = blocked_send
    pending = asyncio.create_task(manager.send_audio_chunk(b"in flight"))
    await asyncio.sleep(0)
    await manager.cancel_ongoing_response()
    release.set()
    assert not await pending
    assert not manager._context_prepared and not manager._has_input
    assert not manager._turn_first_audio
    assert not await manager.send_audio_chunk(b"late")


@pytest.mark.asyncio
async def test_unidentified_cancelled_reply_does_not_block_new_context_transport(manager):
    use_server_vad(manager)
    assert await manager.send_turn_context(42, "Alice")
    await manager.cancel_ongoing_response()
    assert manager._cancel_pending_response
    # No old reply/turn_complete arrives; new identity still goes over transport.
    assert await manager.send_turn_context(43, "Bob")
    assert await manager.send_audio_chunk(b"new owner")
    assert manager._cancel_pending_response  # Attribution remains conservative.


@pytest.mark.asyncio
async def test_server_empty_context_close_preserves_connection(manager):
    use_server_vad(manager)
    session = await manager._get_active_session()

    async def acknowledge(**kwargs):
        await manager._dispatch_message({"server_content": {"turn_complete": True}})

    session.send_realtime_input.side_effect = acknowledge
    assert await manager.send_turn_context(42, "Alice")
    assert await manager.finalize_input_and_request_response()
    manager._connection_handler.disconnect.assert_not_awaited()
    assert [next(iter(call.kwargs)) for call in session.send_realtime_input.await_args_list] == ["text"]


async def complete_server_audio_turn(manager):
    use_server_vad(manager)
    session = await manager._get_active_session()

    async def acknowledge(**kwargs):
        if "text" in kwargs:
            await manager._dispatch_message({"server_content": {"turn_complete": True}})

    session.send_realtime_input.side_effect = acknowledge
    assert await manager.send_turn_context(42, "Alice", streaming=True)
    assert await manager.send_audio_chunk(b"first question")
    await manager._dispatch_message({"server_content": {
        "input_transcription": {"text": "Question"},
        "output_transcription": {"text": "Answer"}, "turn_complete": True,
    }})
    session.send_realtime_input.side_effect = None
    session.send_realtime_input.reset_mock()
    return session


@pytest.mark.asyncio
async def test_identical_sent_context_reuses_server_stream_after_answer(manager):
    session = await complete_server_audio_turn(manager)
    previous_turn = manager._observation_turn
    manager._capture = MagicMock()
    # Reproduce a provider that sends no second metadata response at all.
    assert await manager.send_turn_context(42, "Alice", streaming=True)
    assert manager._observation_turn != previous_turn
    session.send_realtime_input.assert_not_awaited()
    assert manager._context_prepared and manager._activity_open
    assert not manager._response_completed.is_set()
    assert await manager.send_audio_chunk(b"next question")
    assert [next(iter(call.kwargs)) for call in session.send_realtime_input.await_args_list] == ["audio"]
    assert manager._pending_response_contexts[-1]["turn_id"] == manager._observation_turn
    assert manager._capture.call_args_list[0].args[0] == "turn.context.reused"
    assert manager._capture.call_args_list[0].kwargs["reason"] == "identical_sent_context"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["speaker", "snapshot", "epoch", "cancelled", "transport_lost"])
async def test_context_reuse_requires_exact_identity_snapshot_epoch_and_valid_ack(manager, change):
    session = await complete_server_audio_turn(manager)
    user_id, display_name = 42, "Alice"
    if change == "speaker":
        user_id, display_name = 43, "Bob"
    elif change == "snapshot":
        manager._context.turn_context = MagicMock(return_value=manager._last_context_text + "\nupdated snapshot")
    elif change == "epoch":
        manager.connection_epoch += 1
    elif change == "cancelled":
        await manager.cancel_ongoing_response()
    else:
        await manager._transport_lost()
    session.send_realtime_input.side_effect = RuntimeError("failed changed context write")
    assert not await manager.send_turn_context(user_id, display_name, streaming=True)
    assert not await manager.send_audio_chunk(b"must stay blocked")
    assert any("text" in call.kwargs for call in session.send_realtime_input.await_args_list)


@pytest.mark.asyncio
async def test_failed_context_cannot_be_reused_even_when_prior_snapshot_returns(manager):
    session = await complete_server_audio_turn(manager)
    original_text = manager._last_context_text
    session.send_realtime_input.side_effect = RuntimeError("failed write")
    manager._context.turn_context = MagicMock(return_value=original_text + "\nchanged")
    assert not await manager.send_turn_context(42, "Alice")
    manager._context.turn_context.return_value = original_text
    assert not await manager.send_turn_context(42, "Alice")
    assert not await manager.send_audio_chunk(b"blocked after failed update")
