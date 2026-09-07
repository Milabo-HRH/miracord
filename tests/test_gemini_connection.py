"""Offline SDK lifecycle and playback safety tests for Gemini Live."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ai_services.providers.gemini.connection import GeminiRealtimeConnection
from src.ai_services.providers.gemini.event_handler import (
    GeminiEventHandlerAdapter, TurnStartEvent, TurnMessageEvent, TurnEndEvent,
)
from src.exceptions import AIConnectionError


def message(*, audio=None, text=None, tool=False, input_text=None,
            interrupted=False, complete=False):
    content = SimpleNamespace(
        model_turn=object() if audio or text else None,
        output_transcription=None, input_transcription=input_text,
        interrupted=interrupted, turn_complete=complete,
    )
    return SimpleNamespace(server_content=content, data=audio, text=text,
                           tool_call=object() if tool else None,
                           go_away=None, usage_metadata=None)


class FakeSession:
    def __init__(self, *batches):
        self.batches = asyncio.Queue()
        for batch in batches:
            self.batches.put_nowait(batch)
        self.receive_calls = 0
        self.waiting = asyncio.Event()

    async def receive(self):
        self.receive_calls += 1
        self.waiting.set()
        batch = await self.batches.get()
        if isinstance(batch, Exception):
            raise batch
        for item in batch:
            yield item


@pytest.mark.asyncio
async def test_invalid_setup_does_not_retry_or_expose_payload():
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close
    error = ConnectionClosedError(Close(1007, "Invalid JSON private payload"), None)
    connection, context = connection_for(None)
    context.error = error
    await connection._connection_logic()
    assert connection.terminal_error_code == "invalid_request"
    assert connection._shutdown_signal.is_set()
    fields = connection.observer.emit.call_args.kwargs
    assert fields["close_code"] == 1007
    assert "private" not in str(fields)


class FakeContext:
    def __init__(self, session=None, error=None):
        self.session, self.error = session, error
        self.closed = False

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.session

    async def __aexit__(self, *_args):
        self.closed = True


def connection_for(session):
    client = MagicMock()
    context = FakeContext(session)
    client.aio.live.connect.return_value = context
    connection = GeminiRealtimeConnection(client, "test-model", {})
    connection.observer = MagicMock()
    connection._on_connect_callback = AsyncMock()
    connection._on_disconnect_callback = AsyncMock()
    return connection, context


@pytest.mark.asyncio
async def test_idle_receive_and_input_transcription_do_not_start_response():
    session = FakeSession([message(input_text="private input")])
    connection, context = connection_for(session)
    events = []
    connection._event_callback = AsyncMock(side_effect=events.append)
    await connection.connect(connection._event_callback,
                             connection._on_connect_callback,
                             connection._on_disconnect_callback)
    await asyncio.wait_for(connection.wait_until_connected(), 1)
    while not events:
        await asyncio.sleep(0)
    assert all(isinstance(event, TurnMessageEvent) for event in events)
    await asyncio.wait_for(connection.disconnect(), .5)
    assert context.closed
    assert not connection.is_connected()
    connection._on_disconnect_callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_only_receive_does_not_finish_and_audio_completes_same_turn():
    session = FakeSession([message(tool=True)], [message(audio=b"pcm"), message(complete=True)])
    connection, _ = connection_for(session)
    events = []

    async def dispatch(event):
        events.append(event)
        if isinstance(event, TurnEndEvent):
            connection._shutdown_signal.set()

    connection._event_callback = dispatch
    await connection._connection_logic()
    assert [type(event) for event in events] == [
        TurnStartEvent, TurnMessageEvent, TurnMessageEvent,
        TurnMessageEvent, TurnEndEvent,
    ]
    assert events[0].turn_id == events[-1].turn_id
    assert session.receive_calls == 2


@pytest.mark.asyncio
async def test_empty_receive_is_connection_loss_not_busy_loop():
    session = FakeSession([])
    connection, context = connection_for(session)
    connection._event_callback = AsyncMock()
    with pytest.raises(AIConnectionError, match="Gemini transport failed"):
        await connection._connection_logic()
    assert session.receive_calls == 1
    assert context.closed
    connection._event_callback.assert_not_awaited()
    connection._on_disconnect_callback.assert_awaited_once()
    assert connection.get_active_session() is None


@pytest.mark.asyncio
async def test_reconnect_notifies_both_lifecycle_callbacks():
    session1, session2 = FakeSession([]), FakeSession()
    connection, _ = connection_for(session1)
    connection._retry_delay = .001
    connection.gemini_client.aio.live.connect.side_effect = [
        FakeContext(session1), FakeContext(session2),
    ]
    connected_twice = asyncio.Event()
    epochs = []

    async def on_connect():
        epochs.append(len(epochs) + 1)
        if len(epochs) == 2:
            connected_twice.set()

    loss = AsyncMock()
    await connection.connect(AsyncMock(), on_connect, loss)
    await asyncio.wait_for(connected_twice.wait(), 1)
    assert epochs == [1, 2]
    assert loss.await_count == 1
    await connection.disconnect()
    assert loss.await_count == 2


@pytest.mark.asyncio
async def test_quota_failure_stops_retries_and_never_logs_body(caplog):
    class QuotaError(Exception):
        code = 429

    connection, _ = connection_for(FakeSession())
    secret = "private transcript sk-supersecretpassword123"
    connection.gemini_client.aio.live.connect.return_value = FakeContext(
        error=QuotaError(secret)
    )
    await connection.connect(AsyncMock(), AsyncMock(), AsyncMock())
    await asyncio.wait_for(connection._event_loop_task, 1)
    assert connection.terminal_error_code == "resource_exhausted"
    assert connection.gemini_client.aio.live.connect.call_count == 1
    connection._on_disconnect_callback.assert_awaited_once()
    assert secret not in caplog.text
    assert secret not in repr(connection.observer.mock_calls)


@pytest.mark.asyncio
async def test_receive_exception_is_sanitized_and_loss_notified(caplog):
    secret = "private query credential=do-not-log"
    connection, _ = connection_for(FakeSession(RuntimeError(secret)))
    connection._event_callback = AsyncMock()
    with pytest.raises(AIConnectionError) as caught:
        await connection._connection_logic()
    assert secret not in str(caught.value)
    assert secret not in caplog.text
    assert secret not in repr(connection.observer.mock_calls)
    connection._on_disconnect_callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_cancellation_is_received_while_tool_task_is_waiting():
    cancellation = message()
    cancellation.tool_call_cancellation = SimpleNamespace(ids=["call1"])
    session = FakeSession([message(tool=True), cancellation, message(complete=True)])
    connection, _ = connection_for(session)
    pending_tool = None
    tool_cancelled = asyncio.Event()

    async def tool():
        try:
            await asyncio.Event().wait()
        finally:
            tool_cancelled.set()

    async def dispatch(event):
        nonlocal pending_tool
        if isinstance(event, TurnMessageEvent):
            if event.message.tool_call:
                pending_tool = asyncio.create_task(tool())
                await asyncio.sleep(0)
            elif getattr(event.message, "tool_call_cancellation", None):
                pending_tool.cancel()
        if isinstance(event, TurnEndEvent):
            connection._shutdown_signal.set()

    connection._event_callback = dispatch
    await asyncio.wait_for(connection._connection_logic(), 1)
    await asyncio.gather(pending_tool, return_exceptions=True)
    assert tool_cancelled.is_set()


@pytest.mark.asyncio
async def test_disconnect_is_bounded_when_sdk_cleanup_waits():
    session = FakeSession()
    connection, _ = connection_for(session)
    release = asyncio.Event()

    class SlowContext(FakeContext):
        async def __aexit__(self, *_args):
            await release.wait()

    connection.gemini_client.aio.live.connect.return_value = SlowContext(session)
    connection._disconnect_timeout = .01
    await connection.connect(AsyncMock(), AsyncMock(), AsyncMock())
    await asyncio.wait_for(session.waiting.wait(), 1)
    await asyncio.wait_for(connection.disconnect(), .5)
    old_task = connection._event_loop_task
    assert old_task is not None and not old_task.done()
    await connection.connect(AsyncMock(), AsyncMock(), AsyncMock())
    assert connection._event_loop_task is old_task
    release.set()
    await asyncio.gather(old_task, return_exceptions=True)
    await connection.disconnect()
    assert connection._event_loop_task is None
    connection._on_disconnect_callback.assert_awaited_once()


@pytest.fixture
def adapter():
    playback = MagicMock()
    playback.start_new_audio_stream = AsyncMock()
    playback.add_audio_chunk = AsyncMock()
    playback.end_audio_stream = AsyncMock()
    return GeminiEventHandlerAdapter(playback, (24000, 1)), playback


@pytest.mark.asyncio
async def test_interrupted_audio_and_late_output_are_discarded(adapter):
    handler, playback = adapter
    await handler.dispatch_event(TurnStartEvent("one"))
    await handler.dispatch_event(TurnMessageEvent(message(audio=b"first")))
    await handler.dispatch_event(TurnMessageEvent(message(audio=b"discard", interrupted=True)))
    await handler.dispatch_event(TurnMessageEvent(message(audio=b"late")))
    playback.add_audio_chunk.assert_awaited_once_with(b"first")
    playback.interrupt_audio_stream.assert_called_once()
    playback.end_audio_stream.assert_awaited_once()
    await handler.dispatch_event(TurnEndEvent("one"))
    await handler.dispatch_event(TurnStartEvent("two"))
    await handler.dispatch_event(TurnMessageEvent(message(audio=b"next")))
    assert playback.add_audio_chunk.await_args.args == (b"next",)


@pytest.mark.asyncio
async def test_local_cancel_during_audio_startup_discards_first_chunk(adapter):
    handler, playback = adapter
    started, release = asyncio.Event(), asyncio.Event()

    async def start(*_args):
        started.set()
        await release.wait()

    playback.start_new_audio_stream.side_effect = start
    await handler.dispatch_event(TurnStartEvent("one"))
    task = asyncio.create_task(handler.dispatch_event(TurnMessageEvent(message(audio=b"private"))))
    await started.wait()
    await handler.cancel_current_turn()
    release.set()
    await task
    playback.add_audio_chunk.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_does_not_log_raw_transcripts_or_exception_bodies(adapter, caplog):
    handler, playback = adapter
    secret = "sensitive spoken query sk-supersecretpassword123"
    await handler.dispatch_event(TurnStartEvent("one"))
    await handler.dispatch_event(TurnMessageEvent(message(text=secret)))
    playback.add_audio_chunk.side_effect = RuntimeError(secret)
    await handler.dispatch_event(TurnMessageEvent(message(audio=b"pcm")))
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text
