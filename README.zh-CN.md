# MIRA.CORD — 多用户实时 AI 接口

[English](README.md) | [简体中文](README.zh-CN.md)

游戏交互日志与可复现的 prompt/tools 评测见 [OBSERVABILITY.md](OBSERVABILITY.md)。
结构化本地日志默认开启；需要看到具体问答时，设置 `VOICE_DIAGNOSTIC_CAPTURE=true`
并重启 bot。[KEYWORD_RESEARCH.md](KEYWORD_RESEARCH.md) 说明中文模型和
“闭嘴 / 结束”按用户关闭输入的机制。`结束` 只关闭该用户输入，agent
继续回答；`闭嘴` 还会取消该用户当前的回答。关闭后须再次说唤醒词才能恢复输入；
关键词被识别前已经上传的音频无法撤回。

**MIRA** 是 **Multi-user Interface for Realtime AI**（多用户实时 AI 接口）的缩写。MIRA.CORD 是一个 Discord 机器人，可让同一语音频道中的用户与 OpenAI Realtime、xAI Grok Speech-to-Speech、Google Gemini Live，或已登录的 ChatGPT/Codex 桌面语音会话进行共享语音对话。

当前语音通路：通过可选 Pipecat 后端连接 **Gemini 转写 → Flash → 可替换 TTS**，支持 Fish 文字/音频流式输出、连接预热复用和会话边界上下文清理。独立环境与配置见 [PIPECAT.md](PIPECAT.md)。Paraformer 支持可配置中文唤醒/停止词；[SENSEVOICE.md](SENSEVOICE.md) 保留早期 ASR 实验记录。代码默认仍为 Gemini Live，使用新通路需在 `.env` 设置 `AI_SERVICE_PROVIDER=pipecat`。

Gemini、OpenAI 与 Grok 均接入本地对局上下文、Mayhem 只读工具及结构化日志。未知颜色时，强化识别工具一次返回所有颜色的多语言名称；比较工具只比较玩家给出的选项，先查 OP.GG，再按需查询 ARAM Mayhem 数据库。排名仅比较同英雄、同颜色强化。区域名称见 [NAME_GROUNDING.md](NAME_GROUNDING.md)，评测流程见 [OBSERVABILITY.md](OBSERVABILITY.md)，本批改动见 [发布说明](RELEASE_NOTES.md)。

## 功能

- 支持唤醒词和按键录音两种语音交互方式。
- 可选后端原生搜索；Gemini Google 搜索单独默认关闭。
- Gemini/GPT/Grok 在已准入语音之前接收简短对局上下文，缺失信息再查 OP.GG 缓存工具；不猜测发言者对应哪个英雄。
- 可选的 `desktop_voice` 后端通过隔离的 Windows 音频桥接使用个人 ChatGPT/Codex Voice 订阅，无需 OpenAI API Key。
- 每个 Discord 服务器共享一条 AI 会话，同时为每位用户维护独立的本地唤醒门控。
- 可配置 `barge_in`、`hold` 或 `ignore` 三种打断策略。
- 当前发言者可继续说话，无需重复唤醒；换人时需重新说唤醒词。
- 默认自动允许频道内所有用户使用，也可切换为表情授权模式。
- 可通过 Discord 命令即时切换 AI 后端。

## 技术特点

- **并发会话管理**：每个 Discord 服务器使用独立的 `GuildSession`，避免跨服务器状态泄漏。
- **线程安全音频管线**：安全衔接 Discord 的同步音频线程与 asyncio，避免竞争条件。
- **后端无关接口**：可在 Gemini、Grok、OpenAI GA Realtime 和本地桌面语音桥之间切换。
- **可靠连接**：支持指数退避重连与优雅退出。
- **双音频处理路径**：兼顾流式低延迟和批处理质量。
- **容器部署**：API 后端可使用内置 Python 与 FFmpeg 的 Docker 镜像。

## 快速开始

### 1. 前置条件

- **Python 3.11 或更新版本**（已在 Python 3.13 测试）
- **FFmpeg**
  - Windows：`winget install -e --id Gyan.FFmpeg`
  - macOS：`brew install ffmpeg`
  - Debian/Ubuntu：`sudo apt-get install ffmpeg`

使用 `desktop_voice` 还需要：

- Windows；
- 已安装并登录、支持 Voice 的 ChatGPT 或 Codex 桌面应用；
- [Voicemeeter Banana](https://vb-audio.com/Voicemeeter/banana.htm)。安装后需要重启 Windows。

你还需要创建 Discord Bot，并准备所选后端的 API Key。`desktop_voice` 不需要 OpenAI API Key。

#### 创建 Discord Bot

1. 打开 [Discord Developer Portal](https://discord.com/developers/applications)，创建一个 Application。
2. 在 **Installation** 页面将 **Install Link** 设为 **None**，之后使用自定义 OAuth2 邀请链接。
3. 在 **Bot** 页面生成 Token，并妥善保存。不要把 Token 写入代码或提交到 Git。
4. 如果机器人只供自己使用，请关闭 **Public Bot**。
5. 默认斜杠命令不需要 Message Content Intent；只有设置 `ENABLE_PREFIX_COMMANDS=true` 时才需要开启。
6. 在 OAuth2 URL Generator 中选择 `bot` 和 `applications.commands`，并授予以下权限：
   - View Channels
   - Send Messages
   - Add Reactions
   - Connect
   - Speak
7. 使用生成的链接把机器人安装到你的服务器。

API Key 获取地址：

- Gemini：[Google AI Studio](https://aistudio.google.com/app/apikey)
- Grok：[xAI Console](https://console.x.ai/)

### 2. 安装和配置

1. 克隆仓库：

   ```bash
   git clone https://github.com/Milabo-HRH/miracord.git
   cd miracord
   ```

2. 创建并激活虚拟环境。

   Windows：

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\activate
   ```

   macOS/Linux：

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. 安装依赖：

   ```bash
   pip install -r requirements.txt
   ```

4. 配置密钥：

   不想编辑文件时，可在 Windows 运行：

   ```powershell
   py scripts/configure.py
   ```

   脚本不会回显密钥，并会把配置写入被 Git 忽略的 `.env`。也可以手动创建 `.env`：

   ```env
   DISCORD_TOKEN=your_discord_token
   GEMINI_API_KEY=your_gemini_api_key
   XAI_API_KEY=your_xai_api_key
   AI_SERVICE_PROVIDER=gemini
   ```

   只需填写当前所选后端的 Key。`desktop_voice` 使用已登录的桌面应用，不需要 OpenAI API Key。

### 3. 启动机器人

Windows：

```powershell
py main.py
```

macOS/Linux：

```bash
python main.py
```

## 使用方法

### Discord 命令

| 命令 | 说明 | 示例 |
|---|---|---|
| `/connect` | 加入你所在的语音频道并进入待机状态 | `/connect` |
| `/disconnect` | 离开语音频道并重置会话 | `/disconnect` |
| `/set` | 切换后端：`gemini`、`openai`、`grok` 或 `desktop_voice` | `/set desktop_voice` |

这些是 Discord 斜杠命令。全局命令第一次注册时可能需要几分钟才会显示。

### ChatGPT/Codex 桌面 Voice 桥接

它是操作系统音频桥，不是隐藏的订阅 API：

```text
Discord -> Voicemeeter Input -> B1 -> 桌面 Voice 麦克风
桌面 Voice 扬声器 -> Voicemeeter AUX Input -> B2 -> Discord
```

Windows 一次性配置：

1. 安装 Voicemeeter Banana，并重启 Windows。
2. 在 Windows **音量合成器**中，只把 ChatGPT/Codex 的输入路由到 `Voicemeeter Out B1`，输出路由到 `Voicemeeter AUX Input`。
3. 在 Discord **语音与视频**设置中明确选择真实麦克风和耳机/扬声器，不要让 Discord 使用 Voicemeeter B1/B2。
4. 在 ChatGPT 或 Codex 中开启一个 Voice 会话。
5. 在 Discord 使用 `/connect`，然后使用 `/set desktop_voice`。

在同时运行个人 Discord 客户端的电脑上，不要把 B1/B2 设置成 Windows 全局默认录音设备，否则可能替换 Discord 的真实麦克风并形成音频回路。只有不运行个人 Discord 客户端的专用机器人电脑才适合使用全局虚拟默认设备。

后端会启动 Voicemeeter Banana，只把虚拟 Strip 3 路由到 B1，把 Strip 4 路由到 B2，并且只采集 B2。它会在本地识别桌面模型回答的开始与结束，再把音频转发到 Discord，从而避免模型听到自己的回答。

仓库内附的中文关键词检测文件是 `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01` 的最小 INT8 运行子集，以 Apache-2.0 许可证分发。来源和署名信息见模型目录中的 README 与许可证文件。

当前 Windows Voicemeeter 统一驱动配置：

```env
DESKTOP_VOICE_SEND_DEVICE=Voicemeeter Input
DESKTOP_VOICE_RECEIVE_DEVICE=Voicemeeter Out B2
DESKTOP_VOICE_HOST_API=MME
DESKTOP_VOICE_AUTO_ROUTE=true
```

设置 `DESKTOP_VOICE_ALWAYS_FORWARD=true` 可让桌面桥持续混音转发已准入成员的语音；配合 `VOICE_ACCESS_MODE=implicit` 自动包含频道内非机器人用户。此模式绕过唤醒词、停止词和输入音量门限，由桌面 Voice 判断断句与打断。

一次只能桥接一个桌面 Voice 会话，这与 V1 的 `guild_serial` 共享会话设计一致。桌面应用必须保持运行；如果 Voice 结束或要求用户处理，请在应用中重新开启。

### 语音控制

执行 `/connect` 后，机器人会发送状态消息。支持两种录音方式：

1. **按键录音**
   - 添加 🎙️ 表情：开始录音。
   - 移除 🎙️ 表情：停止录音并把音频发送给 AI。

2. **唤醒词**
   - 默认自动允许当前语音频道内的所有用户，不需要点击 👂。
   - 说“豆包”开始录音。
   - 机器人自动检测你何时停止说话并提交音频。
   - 当前发言者可在活动窗口内继续说话，无需重复唤醒。
   - `CROSS_USER_WAKE_REQUIRED=true` 时，换人需要重新说唤醒词，包括之前已参与过的用户。
   - 默认 `sherpa_onnx` 检测器完全在本机运行，唤醒词可通过 `WAKE_WORD_PHRASE` 配置。

如需恢复 👂 表情授权流程，请设置 `VOICE_ACCESS_MODE=explicit`。

### 共享会话与打断设置

每位参与者的自动语音触发器在一段连续发言中只触发一次。即使其他人正在输入，也会独立跟踪各人的发言状态；回到待命不会把持续的说话声当成新的打断。`ACTIVE_SPEECH_REARM_SILENCE_MS=350` 表示该用户静默后才重新允许触发，兼容 Discord 静默期间不发 RTP 包的情况。这不会提高唤醒词阈值，也不会关闭同用户插话。

默认 `guild_serial` 模式为每个服务器保持一条后端会话和一条输出流。`CROSS_USER_WAKE_REQUIRED=true` 要求换人时说唤醒词，并打断之前的轮次；设为 `false` 可恢复所有活动参与者直接开口的行为。同用户插话与新加入者另有策略配置：

```env
CROSS_USER_WAKE_REQUIRED=true
ACTIVE_PARTICIPANT_SPEECH_POLICY=barge_in
NEW_PARTICIPANT_WAKE_POLICY=hold
GEMINI_GOOGLE_SEARCH_ENABLED=false
CONVERSATION_IDLE_TIMEOUT_SECONDS=10
LIVE_INPUT_SILENCE_TIMEOUT_MS=10000
NATIVE_WEB_SEARCH_MODE=auto
PREFERRED_SEARCH_SOURCES=op.gg,leagueoflegends.com,wiki.leagueoflegends.com,reddit.com/r/ARAM
VOICE_ACCESS_MODE=implicit
WAKE_WORD_ENGINE=sherpa_onnx
WAKE_WORD_PHRASE=豆包
GROK_X_SEARCH_ENABLED=false
```

- `barge_in`：打断当前回答并立刻提交新的语音。
- `hold`：完整录制本地语音，等当前回答结束后再提交。
- `ignore`：丢弃本次输入。

Gemini 默认由服务端自动 VAD 判断一句话何时结束，采用服务提供方默认时序。`LIVE_INPUT_SILENCE_TIMEOUT_MS=10000` 只用于本地输入卡住时的安全退出，十秒会话静默用于待机准入；两者都不是 Gemini 正常开始回答的断句阈值。谁持有发言权、换人是否需要唤醒，是独立的路由规则。原生 Google 搜索由 `GEMINI_GOOGLE_SEARCH_ENABLED=false` 单独默认关闭；Grok 保留原生搜索；`desktop_voice` 使用当前 ChatGPT/Codex Voice 会话可用的搜索能力。V1 不需要 MCP。

### 英雄联盟实时对局 MCP 工具

MIRA.CORD 附带一个可选的只读 MCP 服务，用于读取当前英雄联盟对局。它会直接连接 Riot 仅限本机访问的 Live Client Data API，因此必须与游戏客户端运行在同一台电脑上；不需要 Riot API Key、采集脚本或历史日志。

在仓库根目录启动：

```powershell
py scripts/run_lol_mcp.py
```

在 MCP 客户端中把它配置为 stdio 服务即可。请把下面的路径替换成你的实际绝对路径：

```json
{
  "mcpServers": {
    "miracord-league": {
      "command": "py",
      "args": ["C:\\absolute\\path\\to\\miracord\\scripts\\run_lol_mcp.py"]
    }
  }
}
```

服务提供六个工具：

| 工具 | 返回内容 |
|---|---|
| `get_live_game_state` | 当前模式、阵容、英雄、等级、战绩、装备、召唤师技能、Mayhem 强化和近期事件 |
| `get_live_game_events` | 近期对局事件，可通过 `limit` 指定 1 至 50 条 |
| `get_live_game_status` | 轻量检查当前是否存在可读取的对局 |
| `get_mayhem_build` | 缓存的 OP.GG Mayhem 出门装、鞋子和核心出装路线 |
| `get_mayhem_augments` | 保留全部 tier 的强化数据，支持 ID/名称查询与分页 |
| `get_mayhem_champion_tier` | 英雄在 Mayhem 的评级与来源排名，与强化 tier 分开 |

每次查询完整状态或事件时都会优先重新调用 `/liveclientdata/allgamedata`。如果实时端口已经关闭，则只读取采集服务留下的 `logs/lol_live_capture/latest.json`，不会加载整段轮询历史。MCP 根据 Riot 的内部字段 `gameMode=KIWI` 与 `mapNumber=12` 识别 ARAM Mayhem，并从强化技能元数据中解析已观察到的基础、2 阶和 3 阶强化。

服务不会返回玩家名称、Riot ID、账号 ID、PUUID 或无法确认身份的事件参与者。已知参与者会被改写成 `order-01`、`chaos-01` 这样的临时槽位。只有在既没有实时对局、也没有最后快照时，工具才会返回稳定的 `not_in_game`，而不是抛出 MCP 错误。

Live Client API 只会在对局通过 2999 端口暴露数据时可用，而且 Riot 可能省略部分模式专属字段；因此只能报告当前快照实际包含的强化。MCP 工具目前已能向支持 MCP 的模型提供对局上下文，但 `desktop_voice` 音频桥还不会自动把 MCP 返回内容注入 ChatGPT/Codex Voice 会话。

### 带缓存的 OP.GG Mayhem MCP 适配器

Gemini/GPT/Grok 的语音提示词区分 **augment / 海克斯 / 强化** 与商店装备，定位为**数据库查询器**：查到后简短报告名字、评分或效果，不自行分析强弱。默认选择按数据源列表顺序并明确说明；`sourceOrder` 在过滤后仍保留原始列表位置。只缺查询必填条件时追问。语音与独立 MCP 默认 `include_descriptions=false`，问效果或解释时才带描述。`gpt-realtime-2.1` 的低延迟查询模式使用 `OPENAI_REASONING_EFFORT=minimal`。

离线于 Discord 的**付费**提示词测试：`python -m scripts.eval_voice_questions --label lookup`。它使用当前配置的 OpenAI 模型、真实 OP.GG 工具、合成对局上下文和文字问题，将实际语音转写与工具结果存入 `logs/prompt-eval-lookup.{md,json}`，不加入频道，也不使用麦克风或扬声器。请逐题审阅：工具路由检查不等于资料正确性、语音识别、唤醒词或频道实测验收；审阅期间保持 bot 下线。

这是本地兼容修复，没有修改 OP.GG 托管的服务。它直接读取公开的 **ARAM Mayhem** 页面：此前实测官方强化工具遗漏了 tier 0–2，英雄分析工具也不接受 Mayhem 模式。本地适配器保留全部记录，包括缺少名称或描述的记录，绝不会悄悄替换成普通 ARAM 数据。

三个 OP.GG 工具都接受英文英雄名或 OP.GG slug，例如 `Samira`、`Ezreal`、`Kai'Sa`。`get_mayhem_build` 传入 `{"champion":"samira"}` 即可。`get_mayhem_augments` 优先只查询当局提供的强化 ID：

```json
{"champion": "samira", "augment_ids": [1077, 1336, 1356], "include_descriptions": true}
```

也可以用 `query` 搜索名称或内部 key。默认只返回 12 条且不带描述，以减少模型输入量；通过 `offset`、`limit`（1–200）和返回的 `nextOffset` 可以分页读完。分页保留来源顺序，不按 tier 筛掉数据；`totalAvailable`、`totalMatched`、`missingIds` 会明确说明总数、匹配数与缺失项。不会把 performance 冒充胜率，不虚构样本数，也不保证热门出装最优。数据链路没有额外模型调用或搜索循环。

缓存有效期为六小时，保存在内存和被 Git 忽略的 `logs/opgg_cache/` 中，重启 MCP 后可复用。同一进程内的同页并发请求会合并。上游故障时，最多可回退到 24 小时内的缓存，并标明 `cache.stale=true`、原补丁号和抓取时间；更旧的数据不返回。失败重试退避 60 秒，页面结构异常明确报错。

已经注册 `miracord-league` 的客户端只需重启/刷新该 MCP 服务，即可发现新增工具。在 Codex 的 MCP 设置中重启此服务即可，不需要重启 Discord bot。首次注册示例：

```powershell
codex mcp add miracord-league -- python "C:\absolute\path\to\miracord\scripts\run_lol_mcp.py"
```

MCP 客户端自行启动 stdio 进程，这些数据工具不需要开放端口、OP.GG 账号或 API Key。Gemini/GPT/Grok API 适配器直接复用相同 Python 数据适配器，不需要额外启动 MCP 进程或暴露远程 MCP 地址。桌面 Voice 仍不自动注入这些内容。

### Gemini Live 语音与对局上下文

默认配置：

```dotenv
AI_SERVICE_PROVIDER=gemini
GEMINI_MODEL=gemini-3.1-flash-live-preview
GEMINI_GOOGLE_SEARCH_ENABLED=false
CROSS_USER_WAKE_REQUIRED=true
LEAGUE_CONTEXT_ENABLED=true
LEAGUE_TOOLS_ENABLED=true
```

在本地填写 `GEMINI_API_KEY`。关闭原生 Google 搜索不会移除 League/OP.GG 本地工具。
API 请求消耗配额并可能产生费用；能否使用免费层取决于账户与模型资格，不保证永久免费。
Gemini 默认开启服务端自动 VAD，采用提供方默认时序。身份/对局元数据先于麦克风
PCM 发送；元数据单独触发的回答会被抑制，并等待其 `turn_complete`（最长五秒）后
才上传麦克风音频，避免确认身份的回应播放到频道。超时或上下文发送失败会阻止上传。
这会增加延迟；确认的是回答边界，并不保证模型已正确理解上下文。
`GEMINI_LOCAL_VAD_SILENCE_MS=650` 仅为旧手动 VAD 快照保留，不用于默认 Gemini 断句。

`结束` 只关闭该用户输入，通过 `audio_stream_end` 结束已上传的输入，
不取消 agent 的回答；`闭嘴` 还会取消该用户当前的回答并丢弃过期工具结果。
本实现没有 Gemini 服务端显式取消 RPC，不能保证服务器计算随之停止。

同一套[评测命令](OBSERVABILITY.md)支持 Gemini 原生 prompt/tools 快照、固定工具
结果、路由/回答断言，以及完整 JSONL trace。离线回放不调用模型；显式 `--live`
通过支持的实时文字接口发送固定上下文/history 与问题，不回放 Discord 原始音频。
切换自动 VAD 之前，一次使用固定工具结果的合成 Gemini 真实模型用例已通过，
独立手动活动音频探测也已完成。这些历史结果不代表新的自动 VAD 路径、多人 Discord
实测或关键词声学准确率已经验收。

### GPT / Grok API 语音与对局上下文

选择 `AI_SERVICE_PROVIDER=openai` 或 `grok`，在本地配置对应的 API Key。ChatGPT/Codex 订阅不是 API 凭据。默认值为 `OPENAI_REALTIME_MODEL_NAME=gpt-realtime`、`OPENAI_VOICE=marin`、`GROK_MODEL=grok-voice-think-fast-2.0`、`GROK_REASONING_EFFORT=none`（也可选 `high`）。已有模型环境变量仍优先；OpenAI 使用 GA 协议和 24 kHz PCM，Grok 使用 16 kHz 输入、24 kHz 输出。

如需带推理能力的 OpenAI 语音，可设置 `OPENAI_REALTIME_MODEL_NAME=gpt-realtime-2.1`、`OPENAI_REASONING_EFFORT=medium`。支持 `minimal`、`low`、`medium`、`high`、`xhigh`；旧模型应留空。此选项保留原有音频传输和工具，增加推理强度可能提高延迟和输出费用。官方语音价格为输入 $32/百万 tokens、输出 $64/百万 tokens，与 `gpt-realtime` 相同；文本输出由 $16/百万升至 $24/百万。详见 [OpenAI 模型说明](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)。

- `REALTIME_SERVER_VAD=true`：准入后立即流式上传，由后端判断一句话结束（配置 600 ms 静默），本地仍保留 10 秒安全门控。`hold` 在本地排队，释放后才手动提交。设为 `false` 可使用本地 VAD 整句上传。
- `LEAGUE_CONTEXT_ENABLED=true`：后台每两秒读取本机 `/allgamedata`；每轮输入前发送一段限长、完整的上下文，同一用户连续发言也更新。音频发送路径不等待 HTTP 请求。快照超过六秒、游戏端口关闭或出现 `GameEnd` 时标记不可用，不把上一局最终快照当当前局。
- `OPGG_PREFETCH_ENABLED=true`：最多为本局十个英雄预取出装和强化数据，最多两个并发后台请求，复用现有六小时缓存。只注入简短出装备选，整张强化表留在缓存里；缓存未就绪不阻塞语音。关闭后不主动预取 OP.GG。
- `LEAGUE_TOOLS_ENABLED=true`：向 API 模型提供 `get_live_game_state`、`get_mayhem_build`、`get_mayhem_augments`、`get_mayhem_champion_tier` 函数工具。执行白名单、参数/耗时/输出限制；并行结果全部回填后再续答。打断或重连会废弃旧工具结果。

问“OPGG 上 Ambessa 在 Mayhem 是 T 几？”会调用独立的英雄评级工具，返回 `championTier`、`tierLabel`、`championRank`，共享全英雄表缓存并标注来源补丁与新鲜度。强化的 `tier` 是另一套字段，不会混用；不虚构胜率、样本数或玩家段位筛选。语音 API 函数与独立 MCP 都可以调用。

本机对局明确标记为**运行 bot 的电脑提供的共享参考**，不能证明某位 Discord 用户在玩该游戏、属于哪队或使用哪个英雄。游戏账号和 Riot ID 不上传；Discord ID 与显示名用于标记发言者。参与者说话时，这些上下文会发往所选 API。如果不希望提供本机对局，请关闭上下文与预取。关闭原生搜索不等于关闭单独配置的 OP.GG 函数工具。

Grok 保留原生 Web/X 搜索。GPT 新增 `search_web` 函数，复用 `OPENAI_API_KEY` 调用 OpenAI Responses API 的托管搜索；不替换当前语音模型，也不需要另起 MCP 服务。`NATIVE_WEB_SEARCH_MODE=auto` 启用，`off` 移除，与 OP.GG 开关独立。

可以说“联网查一下……”或“去 Reddit 看看……”，最新补丁、新闻、社区讨论也可触发。普通 Mayhem 数据仍优先走 OP.GG 缓存；仅数据库查不到不会自动扩大搜索。指定来源时可限制到 Reddit、Riot、OP.GG、League Wiki 或 arammayhem.com；`auto` 模式的 `PREFERRED_SEARCH_SOURCES` 是偏好，不是严格白名单。

搜索助手默认 `OPENAI_WEB_SEARCH_MODEL=gpt-5.4-mini`、`OPENAI_WEB_SEARCH_REASONING_EFFORT=none`（换无推理模型时留空），每轮发言最多一次托管搜索、最多 700 输出 tokens、不自动重试。`OPENAI_WEB_SEARCH_TIMEOUT_SECONDS=15` 控制总超时，`OPENAI_WEB_SEARCH_CACHE_SECONDS=60` 控制短期内存缓存（0 关闭）。搜索额外计费并增加延迟，连接与待机不会搜索。只给搜索助手发送聚焦后的查询，不附整段语音会话；设置 `store=false`，但这不等于零保留保证。网页仅作为不可信证据，Reddit 明确标记为社区观点；超时、鉴权失败不会被当成“没有这条数据”。实测 `gpt-4.1-mini` 不接受域名 `filters`，因此选择上述搜索助手，语音模型保持不变。

文字频道会收到带可点击引用的搜索摘要（需要发送消息权限）；语音只念简短答案和来源名。搜索运行日志仅记状态、来源数量和耗时，不记录查询、密钥或完整结果。修改配置或代码后重启 bot 生效。

参考：[OpenAI 搜索文档](https://developers.openai.com/api/docs/guides/tools-web-search)、[Realtime 函数工具](https://developers.openai.com/api/docs/guides/realtime-mcp)。可用 `python -m scripts.eval_web_search` 做隔离、付费的真实 API 测试：文字输入、真实语音输出，不连接 Discord 或本机音频设备，不等于语音识别/唤醒词实测。

截图和明确的玩家账号绑定尚未实现。每轮上下文属于计费输入，Grok 还适用文本事件费用。必须收到服务端 `session.updated` 才视为就绪；待机本身不会提交问题或 Discord 音频。

OpenAI 被打断时按 Discord 播放线程已经消费的 PCM 位置截断模型历史，而不是按收到/排队的总音频长度；这不代表远端听众实际听到的精确位置。Grok 会取消生成并丢弃迟到音频，但尚未验证等价的文本历史截断。

运行离线协议测试（仅本地回环，不调用付费模型）：

```powershell
python -m pytest tests/test_realtime_context.py tests/test_grok_provider.py tests/test_ai_service_coordinator.py -q
```

测试中的模型回复是模拟的，不验证真实识别、回答质量、账号额度或频道播放。配置并充值所选 API 后，仍需实际通话验收。重启 bot 才会加载新代码/配置，本次不会自动切换现有实例。

真实协议测试需要联网，但不调用付费模型：

```powershell
python scripts/smoke_test_opgg_mcp.py
python scripts/smoke_test_opgg_mcp.py --champion ezreal
```

## Docker

Docker 仅适用于 API 后端：

```bash
docker build -t miracord .
docker run --env-file .env miracord
```

也可以直接传入环境变量：

```bash
docker run -e DISCORD_TOKEN=xxx -e GEMINI_API_KEY=xxx miracord
```

`desktop_voice` 需要交互式 Windows 音频会话和桌面 Voice 界面，因此不支持 Docker。

## 故障排除

请确认：

- 已激活虚拟环境；
- 已安装 `requirements.txt` 中的依赖；
- `.env` 存在且包含有效密钥；
- Discord Bot 已获得必要权限；
- 启动命令是在项目根目录运行。

## 致谢

- [Google Gemini](https://ai.google.dev/)
- [xAI](https://x.ai/)
- [OpenAI Realtime](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [VB-Audio Voicemeeter](https://vb-audio.com/Voicemeeter/banana.htm)
- [discord.py](https://github.com/Rapptz/discord.py)
- [discord-ext-voice-recv](https://github.com/imayhaveborkedit/discord-ext-voice-recv)
- [openWakeWord](https://github.com/dscripka/openWakeWord)
- [webrtcvad-wheels](https://github.com/wiseman/py-webrtcvad-wheels)
- [NumPy](https://numpy.org/)

## 许可证

本项目使用 MIT 许可证，详见 [LICENSE.md](LICENSE.md)。第三方中文关键词模型文件使用 Apache-2.0 许可证。
