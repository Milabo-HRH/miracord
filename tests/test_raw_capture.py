import json
import time
import wave

from src.audio.raw_capture import RawAudioCapture, claim_raw_capture


def test_capture_preserves_raw_samples_and_separates_speakers(tmp_path):
    directory = tmp_path / 'capture'
    capture = RawAudioCapture(directory)
    capture.submit('anon_a', b'\x01\x02\x03\x04' * 960, rtp_timestamp=100)
    capture.submit('anon_b', b'\x05\x06\x07\x08' * 960, rtp_timestamp=200)
    capture.submit('anon_a', b'\x09\x0a\x0b\x0c' * 960, rtp_timestamp=1060)
    capture.close()
    with wave.open(str(directory / 'anon_a.wav')) as source:
        assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (48000, 2, 2)
        assert source.readframes(source.getnframes()) == b'\x01\x02\x03\x04' * 960 + b'\x09\x0a\x0b\x0c' * 960
    rows = [json.loads(s) for s in (directory / 'frames.jsonl').read_text().splitlines()]
    assert [r['offset_frames'] for r in rows if r['speaker'] == 'anon_a'] == [0, 960]
    assert [r['rtp_timestamp'] for r in rows] == [100, 200, 1060]
    assert rows[0]['arrival_ms'] <= rows[-1]['arrival_ms']
    manifest = json.loads((directory / 'manifest.json').read_text())
    assert manifest['status'] == 'completed' and manifest['dropped_frames'] == 0
    assert len(manifest['files']) == 2


def test_recording_stops_at_deadline_and_rejects_late_audio(tmp_path):
    capture = RawAudioCapture(tmp_path / 'capture', seconds=0.02)
    capture.submit('anon_a', bytes(3840))
    capture._thread.join(timeout=1)
    assert not capture._thread.is_alive()
    before = capture.accepted_bytes
    capture.submit('anon_a', bytes(3840))
    assert capture.accepted_bytes == before
    assert (capture.directory / 'manifest.json').is_file()


def test_recording_is_bounded_and_rejects_unsafe_names(tmp_path):
    capture = RawAudioCapture(tmp_path / 'capture', max_bytes=3840)
    capture.submit('../escape', bytes(3840))
    capture.submit('anon_a', bytes(3840))
    capture.submit('anon_a', bytes(3840))
    capture.close()
    assert capture.accepted_bytes == 3840
    with wave.open(str(capture.directory / 'anon_a.wav')) as source:
        assert source.getnframes() == 960


def test_capture_request_is_consumed_once(tmp_path, monkeypatch):
    monkeypatch.setattr('src.audio.raw_capture.CAPTURE_ROOT', tmp_path)
    assert claim_raw_capture() is None
    (tmp_path / 'capture-next.json').write_text('{"seconds":10}')
    capture = claim_raw_capture()
    assert capture is not None
    try:
        assert not (tmp_path / 'capture-next.json').exists()
        assert claim_raw_capture() is None
    finally:
        capture.close()
