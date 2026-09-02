from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai_services.providers.desktop_voice.manager import (
    DesktopResponseSegmenter,
    DesktopVoiceManager,
)
from src.ai_services.providers.desktop_voice.voicemeeter import VoicemeeterRemote


def _service_config():
    return {
        "processing_audio_frame_rate": 48000,
        "processing_audio_channels": 2,
        "response_audio_frame_rate": 48000,
        "response_audio_channels": 2,
        "send_device": "Voicemeeter Input",
        "receive_device": "Voicemeeter Out B2",
        "preferred_host_api": "Windows WASAPI",
        "frame_ms": 20,
        "response_start_ms": 60,
        "response_silence_ms": 40,
        "response_preroll_ms": 100,
        "vad_aggressiveness": 1,
        "auto_route": False,
        "voicemeeter_remote_dll": "unused",
    }


def test_response_segmenter_preserves_preroll_and_ends_on_silence():
    segmenter = DesktopResponseSegmenter(
        frame_ms=20,
        start_ms=60,
        silence_ms=40,
        preroll_ms=100,
    )

    assert segmenter.feed(b"a", is_speech=False) == []
    assert segmenter.feed(b"b", is_speech=True) == []
    assert segmenter.feed(b"c", is_speech=True) == []
    events = segmenter.feed(b"d", is_speech=True)
    assert [event.kind for event in events] == ["start", "audio"]
    assert events[1].audio == b"abcd"
    assert segmenter.active is True

    assert [e.kind for e in segmenter.feed(b"e", is_speech=False)] == ["audio"]
    assert [e.kind for e in segmenter.feed(b"f", is_speech=False)] == [
        "audio",
        "end",
    ]
    assert segmenter.active is False


def test_device_resolution_prefers_wasapi(monkeypatch):
    devices = [
        {
            "name": "Voicemeeter Input",
            "hostapi": 0,
            "max_output_channels": 2,
            "max_input_channels": 0,
        },
        {
            "name": "Voicemeeter Input",
            "hostapi": 1,
            "max_output_channels": 2,
            "max_input_channels": 0,
        },
    ]
    monkeypatch.setattr(
        "src.ai_services.providers.desktop_voice.manager.sd.query_devices",
        lambda: devices,
    )
    monkeypatch.setattr(
        "src.ai_services.providers.desktop_voice.manager.sd.query_hostapis",
        lambda: [{"name": "MME"}, {"name": "Windows WASAPI"}],
    )

    assert (
        DesktopVoiceManager.resolve_device(
            name_fragment="voicemeeter input",
            direction="output",
            channels=2,
            preferred_host_api="Windows WASAPI",
        )
        == 1
    )


@pytest.mark.asyncio
async def test_send_audio_chunk_writes_to_virtual_microphone():
    playback = MagicMock()
    manager = DesktopVoiceManager(playback, _service_config())
    manager._connected = True
    manager._send_stream = MagicMock()
    manager._send_stream.write.return_value = False

    pcm = b"\x01\x00\x01\x00" * 480
    assert await manager.send_audio_chunk(pcm) is True
    manager._send_stream.write.assert_called_once_with(pcm)


@pytest.mark.asyncio
async def test_cancel_ends_only_current_discord_response():
    playback = MagicMock()
    playback.end_audio_stream = AsyncMock()
    manager = DesktopVoiceManager(playback, _service_config())
    manager._connected = True
    manager._active_response_id = "desktop-test"

    assert await manager.cancel_ongoing_response() is True
    playback.end_audio_stream.assert_awaited_once()
    assert manager._active_response_id is None


def test_voicemeeter_routes_send_and_receive_to_separate_buses():
    dll = MagicMock()
    dll.VBVMR_Login.return_value = 0
    dll.VBVMR_IsParametersDirty.return_value = 0
    dll.VBVMR_SetParameterFloat.return_value = 0
    remote = VoicemeeterRemote("unused", dll=dll)

    remote.connect_and_route()

    routed = {
        call.args[0].decode("ascii"): call.args[1].value
        for call in dll.VBVMR_SetParameterFloat.call_args_list
    }
    for strip_index in range(3):
        assert routed[f"Strip[{strip_index}].B1"] == 0.0
        assert routed[f"Strip[{strip_index}].B2"] == 0.0
    assert routed["Strip[3].B1"] == 1.0
    assert routed["Strip[3].B2"] == 0.0
    assert routed["Strip[4].B1"] == 0.0
    assert routed["Strip[4].B2"] == 1.0
    assert routed["Strip[3].A1"] == 0.0
    assert routed["Strip[4].A1"] == 0.0

    remote.close()
    dll.VBVMR_Logout.assert_called_once()


def test_voicemeeter_starts_banana_when_not_running():
    dll = MagicMock()
    dll.VBVMR_Login.return_value = 1
    dll.VBVMR_RunVoicemeeter.return_value = 0
    dll.VBVMR_IsParametersDirty.return_value = 0
    dll.VBVMR_SetParameterFloat.return_value = 0
    remote = VoicemeeterRemote("unused", start_delay_seconds=0, dll=dll)

    with patch(
        "src.ai_services.providers.desktop_voice.voicemeeter.time.sleep"
    ) as sleep:
        remote.connect_and_route()

    dll.VBVMR_RunVoicemeeter.assert_called_once_with(2)
    sleep.assert_called_once_with(0)
