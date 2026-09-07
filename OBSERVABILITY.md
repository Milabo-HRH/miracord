# Local gameplay diagnostics and repeatable evaluation

Champion nickname/ID and localized augment grounding now uses a local catalog.
The effective `nameGlossary` is captured with turn context; lookup-tool results
are captured like other tools. See [NAME_GROUNDING.md](NAME_GROUNDING.md) for
sources, explicit refresh, ambiguity handling and the observed-only context scope.

Structured events are written to `logs/interactions.jsonl` by default. This is a
local file, not a hosted dashboard. The bot must restart after code/config changes.
The default stream contains correlation IDs, lifecycle events, tool names,
durations, statuses and cache flags. It does not contain conversation text.
`response.audio.first_queued` measures first audio queued locally, not the moment
the user hears it. Discord IDs use process-local pseudonyms in structured traces.

To investigate what was said during a game, set these in the ignored `.env` before
starting the bot:

```dotenv
VOICE_OBSERVABILITY_ENABLED=true
VOICE_DIAGNOSTIC_CAPTURE=true
```

Diagnostic capture adds redacted user/assistant transcripts, effective session
configuration, speaker/game context, and bounded tool arguments/results. It enables
provider transcription for Gemini, OpenAI and Grok. API and transcription
usage consume provider quota and may incur charges; eligible Gemini free-tier
quota may apply. Neither unlimited free use nor universal paid billing is assumed. It never records PCM. Redaction is best effort: conversation text can still
contain personal information. Keep captures, exports and reports under ignored
`logs/`; review them before sharing. Disable capture when the investigation ends.
Files rotate and old files are eventually removed; copy needed traces locally
before they rotate. A missing or truncated capture cannot reproduce the full turn.
The exporter conservatively refuses a connection epoch containing ambiguous
response attribution or reported dropped events; inspect such a trace manually.
During overlapping Gemini activities, transcripts or responses without a reliable
speaker boundary are marked ambiguous instead of assigned to the newest speaker.
Ordinary takeover keeps conversation history and flushes the old user's pending
audio under their old context, with a two-second deadline logged if truncated.
For legacy manual-VAD snapshots, a zero-audio activity may require reconnecting
to avoid Gemini's invalid empty activity-end error; that exceptional reset loses
the provider's unsaved history. The default automatic-VAD path does not open these
manual activities.

Run commands below from the repository root. No command connects to Discord or
controls microphones/speakers. Only `run --live` connects to a model API and
consumes its quota. The CLI defaults to Gemini; saved snapshots retain their
explicit provider and model.

```powershell
# See wake/stop, context, model response, tool call and error timelines.
python -m scripts.eval_interactions inspect logs/interactions.jsonl
python -m scripts.eval_interactions inspect logs/interactions.jsonl --turn TURN_ID

# Export a completed turn while its configuration/transcripts/tool results remain in the log.
python -m scripts.eval_interactions export logs/interactions.jsonl --turn TURN_ID --output logs/cases/game-question.json

# Offline replay without model API use: recorded tool calls traverse the real production response/tool manager.
python -m scripts.eval_interactions run logs/cases/game-question.json --output logs/evals/baseline-offline.json

# A checked-in synthetic example works without API keys or a captured game.
python -m scripts.eval_interactions run tests/fixtures/interactions/gemini_champion_tier.json --output logs/evals/synthetic.json

# Freeze the current complete effective production prompt/tool schemas (no network).
python -m scripts.eval_interactions snapshot --provider gemini --output logs/evals/production.json

# Create a candidate with a reviewed complete prompt and/or function definitions.
python -m scripts.eval_interactions snapshot --provider gemini --instructions logs/evals/candidate-prompt.txt --tools logs/evals/candidate-tools.json --output logs/evals/candidate.json

# Explicit, bounded model API evaluation with frozen local tool results (quota use).
python -m scripts.eval_interactions run logs/cases/game-question.json --live --output logs/evals/baseline.json
python -m scripts.eval_interactions run logs/cases/game-question.json --snapshot logs/evals/candidate.json --live --output logs/evals/candidate-result.json
python -m scripts.eval_interactions compare logs/evals/baseline.json logs/evals/candidate-result.json --output logs/evals/comparison.json
```

Each `run` also writes a companion `.trace.jsonl` next to the report; `--trace`
selects another local path. It uses the same diagnostic schema as production and
can be inspected and exported with the same commands. Gemini snapshots contain
native `system_instruction` and `tools[].function_declarations`. `--instructions`
replaces the full effective instruction text. `--tools` accepts native Gemini
function declarations or the shared flat `type: function` definitions and stores
the resulting native schema. OpenAI/Grok snapshots retain their own protocol.

Each report records input/configuration hashes, selected prompt and tools, actual
tool calls, fixture misses, answers, elapsed time and explicit assertions.
`expected.tool_names` checks exact calls (including duplicates);
`expected.answer_contains` and `expected.answer_not_contains` are optional literal
checks for live runs. Change these expectations deliberately when fixing a bad
recorded route. A captured bad route is a baseline, not an assertion of correctness.
Tool execution changes can be evaluated by editing the frozen result fixture, then
running separate reports. The suite does not execute production adapter code in
place of frozen results; adapter correctness remains covered by its own tests.

Argument matching is exact after JSON parsing. A changed tool call must have a
corresponding reviewed fixture; otherwise evaluation returns `replay_fixture_miss`
and fails. There is no silent live OP.GG/web fallback. Native hosted tools,
including Gemini Google Search and Grok hosted search, cannot consume frozen
results and are rejected before replay connects. Use a reviewed function-only
snapshot. Gemini defaults to `GEMINI_GOOGLE_SEARCH_ENABLED=false`; this does not
remove its local League/OP.GG functions. The live runner disables server VAD for
OpenAI/Grok, caps OpenAI output at 3072 tokens, bounds the response timeout, and
records an effective-session hash. Gemini text replay uses one supported realtime
text message containing the frozen context/history prefix and current question.
It does not use OpenAI `response.create` or an empty manual audio activity.

Offline replay verifies orchestration and results-before-continuation ordering;
it does not predict a modified prompt's choices or grade answer correctness. Live
evaluation runs the selected prompt and tool definitions against a real model but
uses text instead of audio. Transcription may differ from the model's perception
of speech. No mode reproduces Discord transport, acoustic conditions, wake-word
accuracy or prior unrecorded conversation. Add required preceding turns as
`history: [["user", "..."], ["assistant", "..."]]` in a reviewed local fixture.
For Gemini, exported `context_text` preserves the original native context prefix
without wrapping it again on every replay. The exporter includes the effective
speaker/game context; subsequent VAD segments may reuse context already sent on
the same connection. OpenAI/Grok identity-before-audio means ordered successful
WebSocket sends, not a provider processing acknowledgement. Default Gemini also
awaits the successful metadata transport write before microphone PCM. It no longer
waits for a model response to metadata: the instruction to wait for speech can
make that response never arrive. Identical sent context may be reused on the same
connection. Failed sends and changed connection/speaker/gate state block upload.
Output/tools identified before the first PCM remain suppressed. Gemini does not
provide a silent mid-session context acknowledgement; metadata first appearing
after PCM cannot be deterministically distinguished from the answer. This is a
transport-order guarantee, not proof of model understanding or processing order.
Failed streaming input returns to standby without replaying stale unsent PCM.

Gemini now uses automatic provider VAD with provider-default timing for normal
sentence endings. `GEMINI_LOCAL_VAD_SILENCE_MS=650` applies only to legacy
manual-VAD snapshots. The local ten-second timeout is an idle/stuck-input safety
mechanism, not a normal response endpoint. Floor ownership is handled separately.
Native-VAD input remains open during agent playback and subsequent speech by the
same admitted owner. Starting a response no longer closes the microphone upload.
After a standby/idle boundary, bounded in-memory onset preroll preserves the start
of speech; it is uploaded only after current identity/gate validation.
`CROSS_USER_WAKE_REQUIRED=true` lets only the current speaker continue without a
wake phrase. A different user must wake again to take over, even if they spoke
earlier. Per-user stop gates remain closed until that same user's next wake.
`结束` close only the speaker's input: `audio_stream_end` closes
already-delivered input without cancelling the agent's answer. `闭嘴` additionally cancels that speaker's current
answer. For Gemini, cancellation suppresses local playback and stale tools; it is
not a server-side cancellation RPC or a guarantee that provider computation stops.

For diagnosis, distinguish `tool.started` absence (no local function was selected),
`tool.completed` error/not-found/timeout, `tool.result.submitted` and continuation
events, and input/context failure before audio. Server-hosted tools may be opaque;
absence of a local function is not proof of no hosted search.
An exhausted API credit balance can close the socket before a session is accepted;
`insufficient_quota.credit_balance_exhausted` requires restoring that API account's
quota. Changing prompts or transcription settings cannot resolve this error.

# 中文操作说明

默认日志只记链路元数据；要看到游戏里“我问了什么、agent 回了什么”，在本地
`.env` 设置 `VOICE_DIAGNOSTIC_CAPTURE=true` 后重启 bot。查看、导出与回放命令见上。
日志会记录转写而不是原始声音，保存在被 Git 忽略的 `logs/`，不会自动上传。

复现流程是：找到出问题的 `turn_id` → 导出完整轮次 → 固定工具返回 → 修改完整
prompt 或工具定义 → 对同一份 case 跑基线与候选 → 比较调用、回答和耗时。
离线模式不调用模型 API，只检查程序链路；验证 prompt 是否真的改善回答，需要显式添加
`--live` 调用真实模型并消耗配额。Gemini 是否使用免费层取决于账户和模型资格，不能保证
永久免费；超出适用免费配额或使用计费层时可能产生费用。缺失或被截断的日志会拒绝导出，避免把不完整记录当成完整复现。

Gemini 是当前默认后端。`GEMINI_GOOGLE_SEARCH_ENABLED=false` 单独关闭原生 Google
搜索，仍保留本地 League/OP.GG 工具。实际语音默认由服务端自动 VAD 断句，采用提供方
默认时序；本地十秒仅用于待机/异常输入兜底，发言权路由单独管理。
机器人开始回答时，不再关闭当前用户的音频上传；同一用户可继续说话，由服务端
判断停顿和打断。待机后重新开口时会保留最多一秒开头音频，身份/通道校验通过
后再发送，避免本地检测期间吞掉开头；此缓存只在内存中，不写入录音文件。
`CROSS_USER_WAKE_REQUIRED=true` 要求换人时重新说唤醒词。同一套命令支持 Gemini
原生配置快照、prompt/tools 修改、离线回放、显式真实模型验证，以及完整 trace 导出。
每次 `run` 会在报告旁生成 `.trace.jsonl`；Gemini 导出的 `context_text` 保留身份/对局
上下文原文，固定工具结果不会自动回退到线上查询。

`结束` 只关闭当前用户输入，以 `audio_stream_end` 结束已经上传的输入，
不取消 agent 的回答。
`闭嘴` 还会取消该用户当前的回答。两种情况都需该用户再次说唤醒词才能恢复输入，
其他用户的输入和回答不受该停止词影响。

Gemini 身份元数据先发，等待发送成功后再上传麦克风音频；不再等待模型回复元数据。
相同连接中已发送的相同 context 可复用。发送失败或身份/连接变化仍会拦截音频；
输入失败后回到待命，不把旧缓存当作新问题重传。首段音频之前已识别的元数据输出
和工具调用会被抑制，但发送顺序不等于模型内部处理顺序或正确理解身份。650 ms
本地断句设置只为旧手动 VAD 快照保留，默认自动 VAD 不使用它。

2026-09-04 晚间英雄 tier 故障：23:17:49 的十英雄并行查询触发旧版每批
8 次调用限制，整批返回 `tool_budget_or_disabled`。Gemini 现允许每批最多 12 次、
每轮最多 3 批；超出单批限制只拒绝溢出项，日志明确标为 `tool_budget_exceeded`。
实际模型与 OP.GG 工具验证见本地 `logs/evals/gemini-ten-tiers-live.json`：
10 名英雄全部返回成功，模型逐个给出评级。此验证使用文本输入，未验证 Discord
音频识别；本次回答仍为英语，因此不代表中文回答质量已通过。

当前关键词配置已按用户要求改为全中文：WenetSpeech 3.3M 模型检测“豆包”、
“结束”、“闭嘴”，不再检测 `over`。唤醒词使用普通/轻声“豆”的两种读音，
score 3.0 / threshold 0.05；“闭嘴”也使用 3.0 / 0.05，“结束”使用 1.0 / 0.25。
本地 `logs/evals/wake-chinese-controls.json` 保存最终配置的语音验证：同一合成
样本中四次“豆包”均命中，九段共约一分钟的中英文负样本无关键词命中。
旧双语配置在该正样本为零命中。此结果不能代表玩家实测准确率或停止词召回率。

实时上传可启用 `VOICE_INPUT_GATE_ENABLED=true` 的本地噪声门。
`VOICE_INPUT_GATE_DBFS=-42` 指数字 RMS 音量，不是房间声压分贝；数值越低，
越容易放行轻声。每 20 ms 结合音量与 WebRTC 人声检测，连续 60 ms 满足条件
才开放，保留 80 ms 开头缓冲；开放后下降 6 dB 才考虑关门，并保留约 240 ms
尾音。被拦截的帧替换为静音，仍由 Gemini 服务端判断断句。
此功能作用于已授权用户的实时模型输入，唤醒/停止词仍使用原始本地音频。
不是持续降噪或回声消除，较响的人声/游戏声仍可能通过。更换发言者、关闭输入
或输入版本变化会隔离/丢弃旧缓存；正常结束则先发送已允许的尾音。
`input.gate.audio_summary` 每约一秒音频记录音量峰值、过滤帧数、门限与开关状态，
不保存声音。用 `python -m scripts.eval_input_gate logs/evals/synthetic-doubao.wav`
可复跑本地检查；该合成样本过滤前后均命中 4 次“豆包”，不能代替实战验证。

“闭嘴”游戏漏检后提高到 score 3.0 / threshold 0.05。
本地 `logs/evals/stop-keyword-comparison.json` 保留三个候选的完整关键词表测试：
三个“闭嘴”和一个“结束”均命中，九段负样本无命中；较敏感候选第二次
“闭嘴”提前约 320 ms 命中。旧配置在合成样本也能命中，因此尚未复现玩家漏检。

## Explicit one-shot raw audio capture

Raw capture is off by default and separate from transcript logging. After the
user explicitly requested recording for wake-word diagnosis, a one-shot
`logs/raw_audio/capture-next.json` request with `{"seconds":120}` was consumed by
the next Discord sink. Normal reconnects/restarts cannot re-arm that request.
Capture ends automatically within 120 seconds or a 96 MiB accepted-PCM limit.
Only participants already admitted to the local detector are captured; at most
eight separate pseudonymous speaker WAVs are written by a bounded background queue.
No capture data is uploaded or added to Git.

WAVs contain decoded Discord 48 kHz stereo int16 PCM before resampling, keyword
detection or the provider input gate. They are not microphone hardware samples
or original encrypted Opus packets. Received frames are concatenated unchanged;
`frames.jsonl` retains arrival times, RTP timestamps and WAV offsets so packet gaps
can be reconstructed. `manifest.json` records completion, byte/frame bounds and
queue drops. A wake miss before capture began cannot be recovered retroactively.
