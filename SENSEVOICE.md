# SenseVoice FP16 recording tests

This is a local recording evaluator, not the active Discord input gate. Desktop
The historical experiment used Voice forwarding without keyword detection. Current
deployment and Paraformer configuration are described in PIPECAT.md. No player audio is
uploaded to an ASR service. The evaluator never sends messages or controls mute.

## Installed environment

- Official checkpoint: FunAudioLLM/SenseVoiceSmall, revision recorded in
  `logs/sensevoice/source.json`; original files in `logs/sensevoice/model`.
- FunASR 1.4.14 in `logs/sensevoice/venv`, inheriting the existing CUDA PyTorch
  2.3.0 / CUDA 12.1 installation. Bot dependencies are unchanged.
- CUDA device 0, RTX 4080 SUPER; floating model parameters verified float16.
  Inference uses inference_mode and CUDA FP16 autocast. No silent CPU fallback.
- No additional VAD or speaker identification models. Discord already identifies
  the source user; this file test evaluates one supplied recording at a time.

## Test a recording

Save a WAV (recommended), FLAC, OGG or MP3 of at most 60 seconds into
`logs/sensevoice/inbox`. The running worker processes it once its file size and
modification time are unchanged across scans, and writes
`logs/sensevoice/results/<filename>.json`. The originals remain in the inbox.
Audio is converted to mono 16 kHz locally. Give recordings unique filenames.

The worker uses a 2-second rolling window every 300 ms, with a first window at
500 ms and a final partial window. Reports contain raw transcripts, exact
keyword candidates (豆包 / 闭嘴 / 结束), inference time, and CUDA allocator
memory figures. Window endpoints are NOT measured word-end-to-trigger latency.
Repeated candidates are suppressed while continuously present; ASR fluctuations
may produce duplicate candidates. Candidates are not production control actions:
negation, quoted commands and speaker intent are not classified by this matcher.

The model must first transcribe the keyword correctly. Do not add broad homophone
matches just to make a test pass. For example, a synthetic whole-clip test decoded
结束 as 结书, whereas overlapping windows recovered 结束. Real voice accuracy is
still to be established with labeled recordings and negative samples.

Suggested recordings: isolated commands with 1–2 second pauses; commands followed
immediately by a question; normal conversation without commands; similar-sounding
words such as 豆瓣 / 都把 / 结算; a recording with normal game background sound.

## Run or restart

Use the project working directory and its isolated interpreter:

```powershell
logs/sensevoice/venv/Scripts/python.exe scripts/sensevoice_fp16.py --watch
logs/sensevoice/venv/Scripts/python.exe scripts/sensevoice_fp16.py --rolling path/to/clip.wav
```

The watch process stays loaded on the GPU until stopped. It is not configured to
autostart after Windows reboots. Check the fresh heartbeat and PID in
`logs/sensevoice/status.json`, not merely the presence of the file. Only run one
watch process. Logs, recordings, model weights and the environment are git-ignored.

## Initial smoke results

On this RTX 4080 SUPER, after warmup: 6.04 seconds of synthetic speech transcribed
in 96.64 ms; 3.801 seconds in 60.56 ms. Rolling-window totals were 1207.02 ms and
830.92 ms respectively. All three keyword types appeared in rolling transcripts.
CUDA allocated peak was about 465–474 MiB; this excludes additional driver/context
memory and is not total process VRAM. These are synthetic smoke checks, not an
accuracy benchmark or a promise of live gameplay trigger latency.

Sources: https://huggingface.co/FunAudioLLM/SenseVoiceSmall and
https://github.com/QwenAudio/SenseVoice .
