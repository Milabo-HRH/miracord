"""Bounded PCM noise gate; no audio is written to disk."""

import math
from collections import deque

import numpy as np
import webrtcvad


class SpeechInputGate:
    """Delay 80 ms to retain onset audio while rejecting short/noisy activity."""

    def __init__(self, sample_rate=16000, *, open_dbfs=-42.0, vad=None):
        if sample_rate not in (8000, 16000, 32000, 48000):
            raise ValueError("Unsupported gate sample rate")
        if not -80 <= open_dbfs <= -6:
            raise ValueError("Gate threshold must be between -80 and -6 dBFS")
        self.sample_rate = sample_rate
        self.open_dbfs = open_dbfs
        self.frame_bytes = sample_rate * 2 // 50
        self.vad = vad if vad is not None else webrtcvad.Vad(2)
        self.pending = deque()
        self.partial = bytearray()
        self.open = False
        self.attack = self.hold = 0
        self.frames = self.muted = 0
        self.peak_dbfs = -96.0

    def process(self, pcm: bytes) -> bytes:
        self.partial.extend(pcm)
        output = bytearray()
        while len(self.partial) >= self.frame_bytes:
            frame = bytes(self.partial[:self.frame_bytes])
            del self.partial[:self.frame_bytes]
            samples = np.frombuffer(frame, dtype='<i2').astype(np.float32)
            rms = float(np.sqrt(np.mean(samples * samples)))
            dbfs = max(-96.0, 20 * math.log10(max(rms, 0.001) / 32768))
            self.peak_dbfs = max(self.peak_dbfs, dbfs)
            # Keep VAD state current even for quiet frames.
            speech = self.vad.is_speech(frame, self.sample_rate)
            threshold = self.open_dbfs - 6 if self.open else self.open_dbfs
            qualifies = speech and dbfs >= threshold
            self.pending.append([frame, self.open])
            if self.open:
                self.hold = 12 if qualifies else self.hold - 1
                if self.hold <= 0:
                    self.open = False
                    self.attack = 0
            else:
                self.attack = self.attack + 1 if qualifies else 0
                if self.attack >= 3:
                    self.open = True
                    self.hold = 12
                    for entry in self.pending:
                        entry[1] = True
            if len(self.pending) > 4:
                output.extend(self._emit())
        return bytes(output)

    def _emit(self) -> bytes:
        frame, admitted = self.pending.popleft()
        self.frames += 1
        self.muted += int(not admitted)
        return frame if admitted else bytes(len(frame))

    def flush(self) -> bytes:
        """Finish delayed audio for an authorized end/handoff, without repeats."""
        output = b''
        if self.partial:
            output = self.process(bytes(self.frame_bytes - len(self.partial)))
        return output + b''.join(self._emit() for _ in range(len(self.pending)))

    def take_metrics(self) -> dict:
        metrics = {"frames": self.frames, "muted_frames": self.muted,
                   "peak_dbfs": round(self.peak_dbfs, 1), "gate_open": self.open,
                   "threshold_dbfs": self.open_dbfs}
        self.frames = self.muted = 0
        self.peak_dbfs = -96.0
        return metrics
