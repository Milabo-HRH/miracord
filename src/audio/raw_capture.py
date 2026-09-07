"""Explicit, one-shot local capture of decoded Discord PCM before processing."""

import json
import logging
import queue
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

from src.observability import get_observer

logger = logging.getLogger(__name__)
CAPTURE_ROOT = Path(__file__).resolve().parents[2] / 'logs' / 'raw_audio'


class RawAudioCapture:
    def __init__(self, directory: Path, *, seconds=120, max_bytes=96 * 1024 * 1024):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=False)
        self.started = time.monotonic()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.deadline = self.started + seconds
        self.max_bytes = max_bytes
        self.accepted_bytes = self.dropped = 0
        self._queue = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name='raw-audio-capture', daemon=True)
        self._thread.start()
        logger.info('Raw audio capture started: directory=%s duration_seconds=%s', directory, seconds)

    def submit(self, speaker: str, pcm: bytes, *, rtp_timestamp=None):
        """Never block the Discord receive thread on filesystem I/O."""
        now = time.monotonic()
        if not pcm or len(pcm) % 4 or self._stop.is_set() or now >= self.deadline:
            return
        if not speaker or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for c in speaker):
            return
        with self._lock:
            if self.accepted_bytes + len(pcm) > self.max_bytes:
                self._stop.set()
                return
            try:
                self._queue.put_nowait((speaker, bytes(pcm), now,
                    rtp_timestamp if isinstance(rtp_timestamp, int) else None))
            except queue.Full:
                self.dropped += 1
                return
            self.accepted_bytes += len(pcm)

    def _run(self):
        files = {}
        error = None
        try:
            with (self.directory / 'frames.jsonl').open('w', encoding='utf-8') as index:
                while True:
                    expired = self._stop.is_set() or time.monotonic() >= self.deadline
                    try:
                        item = self._queue.get(timeout=0 if expired else 0.1)
                    except queue.Empty:
                        if expired:
                            break
                        continue
                    speaker, pcm, arrived, rtp_timestamp = item
                    if speaker not in files:
                        if len(files) >= 8:
                            self.dropped += 1
                            continue
                        wav = wave.open(str(self.directory / f'{speaker}.wav'), 'wb')
                        wav.setnchannels(2)
                        wav.setsampwidth(2)
                        wav.setframerate(48000)
                        files[speaker] = [wav, 0]
                    wav, offset = files[speaker]
                    wav.writeframes(pcm)
                    row = {'speaker': speaker, 'offset_frames': offset,
                           'pcm_frames': len(pcm) // 4,
                           'arrival_ms': round((arrived - self.started) * 1000, 3),
                           'rtp_timestamp': rtp_timestamp}
                    index.write(json.dumps(row) + '\n')
                    index.flush()
                    files[speaker][1] += len(pcm) // 4
        except (OSError, ValueError, wave.Error) as exc:
            error = type(exc).__name__
            logger.warning('Raw audio capture failed: %s', error)
        finally:
            self._stop.set()
            for wav, _ in files.values():
                try:
                    wav.close()
                except OSError:
                    pass
            manifest = {'started_at': self.started_at, 'finished_at': datetime.now(timezone.utc).isoformat(),
                        'status': 'failed' if error else 'completed', 'error': error,
                        'format': 'Decoded Discord PCM: 48000 Hz, stereo, signed 16-bit little-endian',
                        'timing': 'WAV concatenates received frames without inserted silence. frames.jsonl retains arrival times and RTP timestamps for gap-aware replay.',
                        'dropped_frames': self.dropped,
                        'files': {f'{s}.wav': {'pcm_frames': n, 'audio_seconds': n / 48000} for s, (_, n) in files.items()}}
            try:
                (self.directory / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
            except OSError:
                pass
            logger.info('Raw audio capture finished: directory=%s users=%s dropped_frames=%s',
                        self.directory, len(files), self.dropped)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)


def claim_raw_capture():
    """Consume an explicit request once; ordinary restarts never re-arm capture."""
    request = CAPTURE_ROOT / 'capture-next.json'
    if not request.is_file():
        return None
    token = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    claimed = CAPTURE_ROOT / f'request-{token}.json'
    try:
        request.rename(claimed)
        options = json.loads(claimed.read_text(encoding='utf-8'))
        if not isinstance(options, dict):
            raise ValueError('Capture request must be an object')
        seconds = min(120, max(10, int(options.get('seconds', 120))))
        capture = RawAudioCapture(CAPTURE_ROOT / token, seconds=seconds)
        get_observer().emit('diagnostic.raw_audio.started', duration_seconds=seconds, capture_id=token)
        return capture
    except (OSError, ValueError, TypeError):
        logger.warning('Raw audio capture request could not be started.')
        return None
