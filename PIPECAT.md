# 可替换的转写、回答和语音通路

当前 bot 使用这条通路，测试结果和历史配置见下文。

当前组合：Gemini 3.5 Transcribe Live → Gemini 3.8 Flash（low）→ Fish 曼波。
唤醒词：`曼波曼波`。豆包 TTS 2.0 仍可切换，音色 `zh_female_tianmeitaozi_uranus_bigtts`。

## 连接预热与复用

`PIPE_PREWARM=true` 在连接 bot 时准备管线，尚未收到获准的用户音频前不回答。
`PIPE_CONNECTION_LIFETIME_SECONDS=2700` 保留管线至少 45 分钟，到期后等待空闲再回收；服务故障可提前重连。这个配置不改变 10 秒输入窗口，也不会持续上传待机声音。

正常同人重新唤醒复用 STT、LLM 和 TTS 服务实例。换人、打断未完成转写或本局热词变化时，只重连 Gemini 转写，以免旧语音串到新身份；保留对话历史和其余管线。供应商仍可能按自己的连接限制重连，不能保证一个底层 socket 始终存活 45 分钟。中断使用 Pipecat 原生取消传播并等待确认，再清理旧转写聚合和断句计时器。

三轮真实 API 合成音频复用测试：`logs/pipecat/warm-reuse-smoke/report.json`。启动预热 7672 ms；每轮之间关闭输入并等待 11 秒。同人 context 准备两次均小于 1 ms，换人重连转写 328 ms；三轮均复用同一 worker，均收到回答。说完到首段 PCM 为 1578、1687、1359 ms。未包含 Discord 播放，未进行完整 45 分钟浸泡测试；到期和活动轮次延期通过缩短时钟的自动化测试覆盖。

复现命令：

```powershell
$env:PYTHONPATH='.'
& logs/pipecat/venv/Scripts/python.exe -X utf8 scripts/smoke_test_pipecat.py --live --tts-provider fish --rounds 3 --idle-seconds 11 --switch-speaker-last --text '曼波曼波，你好，能听到我说话吗？' --output logs/pipecat/warm-reuse-smoke
```

## 环境与启动

这条通路使用 Pipecat 1.8.1、google-genai 2.22.0。旧环境仍使用 google-genai 1.x，不能把两份 requirements 一起安装。
`requirements-pipecat.txt` 包含独立环境所需的 bot 与通路依赖。新环境可用 Python 3.11 创建 venv 后安装此文件；CUDA PyTorch、Paraformer 模型缓存及 FFmpeg 仍需按现有部署准备。没有验证全新机器的 GPU 安装。

本机已准备 `logs/pipecat/venv/Scripts/python.exe`；该环境继承现有系统依赖，单独安装了 Pipecat、新版 Google SDK 和 FunASR。已确认 FunASR 可导入，未重新测量 GPU 唤醒模型。

准备切换时，用新环境运行 `main.py`，并设置：

```dotenv
AI_SERVICE_PROVIDER=pipecat
PIPE_STT_MODEL=gemini-3.5-transcribe-live
PIPE_STT_LANGUAGES=cmn-Hans-CN,en-US
PIPE_LLM_PROVIDER=google
PIPE_LLM_MODEL=gemini-3.8-flash
PIPE_THINKING=medium
PIPE_TTS_PROVIDER=doubao
PIPE_TTS_MODEL=seed-tts-2.0
PIPE_TTS_VOICE=zh_female_tianmeitaozi_uranus_bigtts
DOUBAO_API_KEY_FILE=.secrets/doubao.txt
PIPE_TURN_SILENCE_SECONDS=0.8
VOICE_DIAGNOSTIC_CAPTURE=true
```

Google 沿用 `GEMINI_API_KEY`。豆包只读取外部文件，不复制密钥到仓库。当前 `.env` 保留豆包密钥路径，并已选 Fish 曼波作为新通路的 TTS；`AI_SERVICE_PROVIDER` 仍为旧通路。

### Fish 曼波

曼波音色页面：https://fish.audio/m/0f08cacd3e354471a4b94dd00b4cc4a3/ 。已接通 Pipecat Fish WebSocket 服务，支持通过本地密钥文件完成认证。

```dotenv
PIPE_TTS_PROVIDER=fish
PIPE_TTS_MODEL=s2.1-pro-free
PIPE_TTS_VOICE=0f08cacd3e354471a4b94dd00b4cc4a3
FISH_API_KEY_FILE=.secrets/fish.txt
```

也支持 `FISH_API_KEY`；它优先于文件。文件只放单行密钥。切换服务时要同时切换 PIPE_TTS_VOICE 和 PIPE_TTS_MODEL，避免沿用豆包的音色或资源名。

可独立复测：

```powershell
$env:PYTHONPATH='.'
& logs/pipecat/venv/Scripts/python.exe -X utf8 scripts/smoke_test_pipecat.py --live --tts-provider fish --tts-voice 0f08cacd3e354471a4b94dd00b4cc4a3 --fish-key-file .secrets/fish.txt --output logs/pipecat/fish-manbo-smoke
```

输入仍由豆包合成，回答使用 Fish 曼波；没有读取麦克风或连接 Discord。本机已成功读取该文件并合成曼波音色。9 月6日实测：转写“豆包，你好。 能听到我说话吗？”，回答“能听到，你说，有什么要查的？”，说完至回答首段 PCM 2156 ms，回答音频2.7秒。报告与试听在 logs/pipecat/fish-manbo-smoke/。这是单次合成输入测试，不能据此断言比豆包更快或已通过游戏实测。

## 服务接口与对话语义

- 回答模型沿用 Pipecat 的 LLMService 接口，在 `services.py` 的 LLM_FACTORIES 注册工厂；当前只有 Google 已接通。
- TTS 沿用 Pipecat 的 TTSService 接口。提供豆包、Gemini、Fish、MiniMax 工厂；豆包和 Fish 曼波完成真实合成测试，Gemini TTS / MiniMax 不宣称实测通过。
- 豆包使用标准 HTTP 分块接口 `/api/v3/tts/unidirectional`。本次账号标准接口成功，SSE 接口曾返回资源权限错误。
- Pipecat 负责转写聚合、停止发言判定和模型/TTS 取消。当前使用 Silero VAD 和 0.5 秒 SpeechTimeout 策略，没有启用 SmartTurn；并非完全依赖 Gemini 服务端断句。
- 需要实际转写才能触发框架内的用户轮次与打断；这不保证背景人声或错误转写不会误触发。
- 首位用户的身份 context 在音频入管线前送入；每次调用回答模型前同步刷新该说话者的对局 context。工具继续使用现有 LeagueToolExecutor、数据库、两工具强化查询和当前 prompt。
- 其他人按现有唤醒规则抢占，取消旧输出、单独重置转写连接并保留已有文本历史。尚未转写完成的旧语音不能保证保留；不把旧音频转挂到新用户。
- “闭嘴”取消当前输出并拒收旧管线的迟到音频；“结束”只结束输入，允许完成回答。关键词识别仍是现有 Paraformer，3.5 转写不负责打开输入闸门。
- `<NO_REPLY>` 仍由 prompt 决定。ReplyGate 只等待开头是否匹配该标记；确认正常文字立即放行。Fish 使用 TOKEN 模式逐片接收文字，不等待全文或整句。其他 TTS 供应商仍采用各自原生分句策略。已经播放的内容无法因后续出现控制标记而撤回。
- Paraformer 读取 `WAKE_WORD_PHRASE`，当前为“曼波曼波”；双词唤醒准确率仍需要真实语音检验。

## 可复现验证

### 流式回答与上下文生命周期（2026-09-06 更新）

- 最新完整 `VOICE_CONTEXT` 替换旧快照；历史问题只保留简短 `SPEAKER_CONTEXT` 标记，避免换人后误归属。
- “闭嘴”取消输出并清空历史、工具结果及追问记忆；普通抢占只打断，不清空历史。“结束”仍只结束输入，允许回答完成。
- 会话监控判定10秒空闲（没有录音、待回答或播放）后，用最多两轮截断原话和最近一次已验证的比较选项替换历史。没有额外摘要模型调用。记忆注明原说话者和旧数据属性，下一轮重新插入当前身份/对局。缺失细节须重新查工具。
- 生成的完整回答写入文字历史一次；TTS 的回传文本不重复写入。该历史代表模型生成过的内容，不保证全部已被听到。
- `response.text.first` 是第一段正常文字放行时间，`transcript.assistant` 是全文完成时间，`response.audio.first` 是首PCM。流式模式下首PCM可以早于全文完成；不要再把全文→首PCM当作纯TTS时延，优先看供应商TTFB和首文字→首PCM。
- `context.compacted` 诊断payload含清理原因、前后消息数和保留字符数。清理不重建语音管线。

脚本增加 `--compact-between idle` / `--compact-between explicit_stop`，配合 `--rounds` 验证边界。真实框架测试强制等首PCM后才发模型完成帧，覆盖真正的全文完成前出声；同时覆盖清理后的第一轮身份、历史与继续回答。

独立脚本不接麦克风、不进 Discord、不读取本局、不开放游戏工具。它先用豆包生成一句话，再把音频实时送入完整通路：

```powershell
$env:PYTHONPATH='.'
& logs/pipecat/venv/Scripts/python.exe -X utf8 scripts/smoke_test_pipecat.py --live --text '豆包，你好，能听到我说话吗？' --output logs/pipecat/new-smoke
```

需调用已配置的真实 API，可能消耗额度。产物包括 input.wav、reply.wav、report.json 和 trace/interactions.jsonl。时间为合成语音发送结束到输出适配器拿到首段 PCM，未包含真实 Discord 播放延迟。

9 月 6 日中文提示版实测：

| 项目 | 结果 |
| --- | --- |
| 转写 | 豆包，你好。 能听到我说话吗？ |
| 回答 | 能听到，你说。想查什么？ |
| 指定音色合成首包 | 781 ms |
| 说完到回答首音 | 2531 ms |
| 回答音频长度 | 1.6 秒 |

结果在 `logs/pipecat/chinese-hints-smoke/`。之前未限制语言的指定音色测试出现日文“とば”；加入中文/英文提示和适配词后，这一句识别正确。样本太少，不能推断真实语音或英雄昵称的准确率。

离线测试覆盖真实 Pipecat worker 下连续两句、历史保留、身份顺序、停止后旧音频、跨令牌 NO_REPLY、旧工具结果丢弃、豆包流读取/错误/取消，以及可配置双唤醒词。游戏问答质量仍需沿用现有冻结样本与人工评审；本次 hello smoke 不能替代游戏 E2E。

日志沿用现有 observability，记录转写、回答、context、工具请求/结果、时延与用量。开启诊断捕获才保存正文；供应商调试日志关闭，密钥按既有规则脱敏。检查 payload_truncated，截断记录不能用于完整重放。
