"""Bounded rolling SeACo decoding, off the Discord and asyncio threads."""
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time
import unicodedata

import numpy as np
import webrtcvad

from src.audio.keyword_events import KeywordEventTracker
from src.observability import get_observer

logger = logging.getLogger(__name__)
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paraformer")
_runtime = None
_load_lock = threading.Lock()


def speech_metrics(audio):
    """Require 120 ms of voiced, audible frames before invoking biased ASR."""
    from src.config.config import Config
    vad = webrtcvad.Vad(2)
    voiced = 0
    peak = -96.0
    for offset in range(0, len(audio)-319, 320):
        frame = audio[offset:offset+320]
        dbfs = max(-96.0, float(20*np.log10(max(float(np.sqrt(np.mean(frame*frame))), 1e-8))))
        peak = max(peak, dbfs)
        pcm = (np.clip(frame, -1, 1)*32767).astype('<i2').tobytes()
        if vad.is_speech(pcm, 16000) and dbfs >= Config.VOICE_INPUT_GATE_DBFS:
            voiced += 20
    return {'voiced_ms': voiced, 'peak_frame_dbfs': round(peak, 1),
            'speech_qualified': voiced >= 120}


def prepare_paraformer():
    """Load once before opening Discord; share GPU weights between speakers."""
    global _runtime
    with _load_lock:
        if _runtime is None:
            from scripts.eval_paraformer_keywords import ParaformerEvaluator
            runtime = ParaformerEvaluator("fp32")
            from src.config.config import Config
            runtime.hotwords = f"{Config.WAKE_WORD_PHRASE} 闭嘴 结束"
            runtime.transcribe(np.zeros(16000, dtype=np.float32))
            _runtime = runtime
            logger.info("Paraformer ready: precision=fp32 device=cuda hotwords=%s/闭嘴/结束", Config.WAKE_WORD_PHRASE)
    return _runtime


class ParaformerWakeWordModel:
    """One bounded audio buffer and at most one queued inference per speaker."""
    def __init__(self, user_id, runtime=None, executor=None):
        from src.config.config import Config
        self.wake_phrase = Config.WAKE_WORD_PHRASE
        self.runtime = runtime or prepare_paraformer()
        self.executor = executor or _pool
        self.user_id = user_id
        self._lock = threading.RLock()
        self._future = None
        self._generation = 0
        self._audio = np.empty(0, dtype=np.float32)
        self._end = self._submitted_end = 0
        self._tracker = KeywordEventTracker()
        self._last_feed = None

    def reset(self):
        with self._lock:
            self._generation += 1
            if self._future is not None:
                self._future.cancel()
            self._audio = np.empty(0, dtype=np.float32)
            self._end = self._submitted_end = 0
            self._tracker = KeywordEventTracker()

    def _decode(self, audio, start, end, generation, submitted):
        metrics = speech_metrics(audio)
        text, elapsed = self.runtime.transcribe(audio) if metrics['speech_qualified'] else ('', 0)
        return text, elapsed, start, end, generation, submitted, metrics

    def poll(self):
        with self._lock:
            result = {}
            if self._future is not None and self._future.done():
                future, self._future = self._future, None
                if not future.cancelled():
                    try:
                        text, elapsed, start, end, generation, submitted, metrics = future.result()
                        if generation == self._generation:
                            clean = ''.join(c for c in unicodedata.normalize('NFKC', text) if c.isalnum())
                            events = self._tracker.consume(start, end, [w for w in (self.wake_phrase, '闭嘴', '结束') if w in clean])
                            observer = get_observer()
                            observer.capture('asr.keyword.window', {
                                'text': text, 'events': events, 'start_sample': start,
                                'end_sample': end, 'inference_ms': elapsed,
                                'queue_and_inference_ms': round((time.monotonic()-submitted)*1000, 2),
                                'precision': 'fp32', 'hotwords': [self.wake_phrase, '闭嘴', '结束'],
                                **metrics,
                            }, speaker_id=observer.pseudonym(self.user_id))
                            result = {word: 1.0 for word in events}
                    except Exception:
                        logger.exception('Paraformer inference failed')
            # Coalesce audio accumulated while inference ran; no growing job queue.
            if (not result and self._future is None and self._audio.size >= 8000
                    and self._end-self._submitted_end >= 4800):
                start = max(self._end-len(self._audio), self._tracker.minimum_start)
                audio = self._audio[start-(self._end-len(self._audio)):].copy()
                if len(audio) >= 8000:
                    self._submitted_end = self._end
                    self._future = self.executor.submit(self._decode, audio, start,
                        self._end, self._generation, time.monotonic())
            return result

    def predict(self, samples):
        with self._lock:
            now = time.monotonic()
            if self._last_feed is not None and now-self._last_feed > 0.9:
                # RTP gaps must not concatenate unrelated phrases across minutes.
                self.reset()
            self._last_feed = now
            normalized = np.asarray(samples, dtype=np.int16).astype(np.float32)/32768.0
            self._end += len(normalized)
            self._audio = np.concatenate((self._audio, normalized))[-32000:]
            return self.poll()
