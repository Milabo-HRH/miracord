"""Minimal controller for the installed Voicemeeter Remote API."""

from __future__ import annotations

import ctypes
import time
from pathlib import Path
from typing import Any

from src.utils.logger import get_logger

logger = get_logger(__name__)


class VoicemeeterError(RuntimeError):
    """Raised when the Voicemeeter engine or remote API is unavailable."""


class VoicemeeterRemote:
    """Start Banana and configure two isolated virtual Voice paths.

    Virtual input strip 3 is routed only to B1. The bot writes Discord audio
    to "Voicemeeter Input" and ChatGPT/Codex selects "Voicemeeter Out B1"
    as its microphone.

    Virtual input strip 4 is routed only to B2. ChatGPT/Codex selects
    "Voicemeeter Aux Input" as its speaker and the bot captures
    "Voicemeeter Out B2".
    """

    BANANA_TYPE = 2

    def __init__(
        self,
        dll_path: Path,
        *,
        start_delay_seconds: float = 2.0,
        dll: Any | None = None,
    ) -> None:
        self._dll_path = Path(dll_path)
        self._start_delay_seconds = start_delay_seconds
        self._dll = dll
        self._logged_in = False

    def _load(self) -> None:
        if self._dll is None:
            if not self._dll_path.is_file():
                raise VoicemeeterError(
                    f"Voicemeeter Remote DLL not found: {self._dll_path}"
                )
            self._dll = ctypes.WinDLL(str(self._dll_path))

        self._dll.VBVMR_Login.restype = ctypes.c_long
        self._dll.VBVMR_Logout.restype = ctypes.c_long
        self._dll.VBVMR_RunVoicemeeter.argtypes = [ctypes.c_long]
        self._dll.VBVMR_RunVoicemeeter.restype = ctypes.c_long
        self._dll.VBVMR_IsParametersDirty.restype = ctypes.c_long
        self._dll.VBVMR_SetParameterFloat.argtypes = [
            ctypes.c_char_p,
            ctypes.c_float,
        ]
        self._dll.VBVMR_SetParameterFloat.restype = ctypes.c_long

    def connect_and_route(self) -> None:
        self._load()
        result = int(self._dll.VBVMR_Login())
        if result < 0:
            raise VoicemeeterError(f"Voicemeeter login failed with code {result}")
        self._logged_in = True

        # The official API returns 1 when no mixer process is running.
        if result == 1:
            run_result = int(self._dll.VBVMR_RunVoicemeeter(self.BANANA_TYPE))
            if run_result < 0:
                raise VoicemeeterError(
                    f"Could not launch Voicemeeter Banana (code {run_result})"
                )
            time.sleep(self._start_delay_seconds)

        dirty_result = int(self._dll.VBVMR_IsParametersDirty())
        if dirty_result < 0:
            raise VoicemeeterError(
                "Voicemeeter started but its audio engine is not ready. "
                "A Windows restart may still be required."
            )

        routes = {
            # Never leak a locally attached microphone into either bridge bus.
            # Discord supplies participant audio through virtual strip 3, so the
            # three Banana hardware strips must remain isolated even if a user
            # previously selected a microphone in the Voicemeeter UI.
            "Strip[0].B1": 0.0,
            "Strip[0].B2": 0.0,
            "Strip[1].B1": 0.0,
            "Strip[1].B2": 0.0,
            "Strip[2].B1": 0.0,
            "Strip[2].B2": 0.0,
            # Bot -> desktop Voice microphone.
            "Strip[3].Mute": 0.0,
            "Strip[3].Gain": 0.0,
            "Strip[3].A1": 0.0,
            "Strip[3].A2": 0.0,
            "Strip[3].A3": 0.0,
            "Strip[3].B1": 1.0,
            "Strip[3].B2": 0.0,
            # Desktop Voice speaker -> bot capture.
            "Strip[4].Mute": 0.0,
            "Strip[4].Gain": 0.0,
            "Strip[4].A1": 0.0,
            "Strip[4].A2": 0.0,
            "Strip[4].A3": 0.0,
            "Strip[4].B1": 0.0,
            "Strip[4].B2": 1.0,
            "Bus[3].Mute": 0.0,
            "Bus[3].Gain": 0.0,
            "Bus[4].Mute": 0.0,
            "Bus[4].Gain": 0.0,
        }
        for name, value in routes.items():
            self._set_float(name, value)
        logger.info("Configured isolated Voicemeeter B1/B2 desktop Voice routes.")

    def _set_float(self, name: str, value: float) -> None:
        result = int(
            self._dll.VBVMR_SetParameterFloat(
                name.encode("ascii"), ctypes.c_float(value)
            )
        )
        if result < 0:
            raise VoicemeeterError(
                f"Could not set Voicemeeter parameter {name!r} (code {result})"
            )

    def close(self) -> None:
        if not self._logged_in or self._dll is None:
            return
        try:
            self._dll.VBVMR_Logout()
        finally:
            self._logged_in = False
