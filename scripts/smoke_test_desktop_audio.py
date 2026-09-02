"""Verify both Voicemeeter desktop Voice paths and their isolation."""

from __future__ import annotations

import time
from typing import Any

import numpy as np
import sounddevice as sd

from src.ai_services.providers.desktop_voice.manager import DesktopVoiceManager
from src.ai_services.providers.desktop_voice.voicemeeter import VoicemeeterRemote
from src.config.config import Config


def resolve(fragment: str, direction: str) -> int:
    return DesktopVoiceManager.resolve_device(
        name_fragment=fragment,
        direction=direction,
        channels=2,
        preferred_host_api=Config.DESKTOP_VOICE_HOST_API,
    )


def measure_route(
    *,
    playback_fragment: str,
    expected_capture_fragment: str,
    isolated_capture_fragment: str,
) -> tuple[float, float]:
    sample_rate = Config.DESKTOP_VOICE_SAMPLE_RATE
    duration = 0.5
    samples = np.arange(int(sample_rate * duration), dtype=np.float32)
    tone = (0.15 * np.sin(2 * np.pi * 523.25 * samples / sample_rate)).astype(
        np.float32
    )
    stereo_tone = np.column_stack((tone, tone))
    expected_chunks: list[np.ndarray[Any, Any]] = []
    isolated_chunks: list[np.ndarray[Any, Any]] = []

    def expected_callback(indata, _frames, _time, _status):
        expected_chunks.append(indata.copy())

    def isolated_callback(indata, _frames, _time, _status):
        isolated_chunks.append(indata.copy())

    with (
        sd.InputStream(
            samplerate=sample_rate,
            channels=2,
            dtype="float32",
            device=resolve(expected_capture_fragment, "input"),
            callback=expected_callback,
        ),
        sd.InputStream(
            samplerate=sample_rate,
            channels=2,
            dtype="float32",
            device=resolve(isolated_capture_fragment, "input"),
            callback=isolated_callback,
        ),
        sd.OutputStream(
            samplerate=sample_rate,
            channels=2,
            dtype="float32",
            device=resolve(playback_fragment, "output"),
        ) as output,
    ):
        time.sleep(0.15)
        output.write(stereo_tone)
        time.sleep(0.25)

    expected = np.concatenate(expected_chunks) if expected_chunks else np.zeros(1)
    isolated = np.concatenate(isolated_chunks) if isolated_chunks else np.zeros(1)
    expected_rms = float(np.sqrt(np.mean(np.square(expected))))
    isolated_rms = float(np.sqrt(np.mean(np.square(isolated))))
    return expected_rms, isolated_rms


def main() -> None:
    remote = VoicemeeterRemote(Config.VOICEMEETER_REMOTE_DLL)
    try:
        remote.connect_and_route()
        send_rms, send_leak = measure_route(
            playback_fragment=Config.DESKTOP_VOICE_SEND_DEVICE,
            expected_capture_fragment="Voicemeeter Out B1",
            isolated_capture_fragment=Config.DESKTOP_VOICE_RECEIVE_DEVICE,
        )
        receive_rms, receive_leak = measure_route(
            playback_fragment="Voicemeeter AUX Input",
            expected_capture_fragment=Config.DESKTOP_VOICE_RECEIVE_DEVICE,
            isolated_capture_fragment="Voicemeeter Out B1",
        )
        print(
            "Desktop audio bridge: "
            f"send={send_rms:.4f}, send_leak={send_leak:.4f}, "
            f"receive={receive_rms:.4f}, receive_leak={receive_leak:.4f}"
        )
        if min(send_rms, receive_rms) < 0.02:
            raise RuntimeError("One of the Voicemeeter routes carried no test tone")
        if max(send_leak, receive_leak) > min(send_rms, receive_rms) * 0.1:
            raise RuntimeError("Voicemeeter B1/B2 isolation test detected crosstalk")
    finally:
        remote.close()


if __name__ == "__main__":
    main()
