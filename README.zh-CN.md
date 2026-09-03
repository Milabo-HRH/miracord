# MIRA.CORD — 多用户实时 AI 接口

[English](README.md) | [简体中文](README.zh-CN.md)

**MIRA** 是 **Multi-user Interface for Realtime AI**（多用户实时 AI 接口）的缩写。MIRA.CORD 是一个 Discord 机器人，可让同一语音频道中的用户与 Google Gemini Live、xAI Grok Speech-to-Speech，或已登录的 ChatGPT/Codex 桌面语音会话进行共享语音对话。

## 功能

- 支持唤醒词和按键录音两种语音交互方式。
- Gemini 和 Grok 是 V1 的一等后端，分别支持原生 Google/Web/X 搜索。
- 可选的 `desktop_voice` 后端通过隔离的 Windows 音频桥接使用个人 ChatGPT/Codex Voice 订阅，无需 OpenAI API Key。
- 每个 Discord 服务器共享一条 AI 会话，同时为每位用户维护独立的本地唤醒门控。
- 可配置 `barge_in`、`hold` 或 `ignore` 三种打断策略。
- 已加入当前对话的用户可继续说话，无需重复唤醒词。
- 默认自动允许频道内所有用户使用，也可切换为表情授权模式。
- 可通过 Discord 命令即时切换 AI 后端。

## 技术特点

- **并发会话管理**：每个 Discord 服务器使用独立的 `GuildSession`，避免跨服务器状态泄漏。
- **线程安全音频管线**：安全衔接 Discord 的同步音频线程与 asyncio，避免竞争条件。
- **后端无关接口**：可在 Gemini、Grok、兼容旧版的 OpenAI Realtime 和本地桌面语音桥之间切换。
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
| `/set` | 切换后端：`gemini`、`grok` 或 `desktop_voice`；`openai` 保留旧版兼容 | `/set desktop_voice` |

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

一次只能桥接一个桌面 Voice 会话，这与 V1 的 `guild_serial` 共享会话设计一致。桌面应用必须保持运行；如果 Voice 结束或要求用户处理，请在应用中重新开启。

### 语音控制

执行 `/connect` 后，机器人会发送状态消息。支持两种录音方式：

1. **按键录音**
   - 添加 🎙️ 表情：开始录音。
   - 移除 🎙️ 表情：停止录音并把音频发送给 AI。

2. **唤醒词**
   - 默认自动允许当前语音频道内的所有用户，不需要点击 👂。
   - 说“豆包豆包”开始录音。
   - 机器人自动检测你何时停止说话并提交音频。
   - 用户加入共享对话后，可在当前活动窗口内继续说话，无需重复唤醒。
   - 其他用户第一次加入时需要先说一次唤醒词。
   - 默认 `sherpa_onnx` 检测器完全在本机运行，唤醒词可通过 `WAKE_WORD_PHRASE` 配置。

如需恢复 👂 表情授权流程，请设置 `VOICE_ACCESS_MODE=explicit`。

### 共享会话与打断设置

默认 `guild_serial` 模式为每个服务器保持一条后端会话和一条输出流。现有参与者与新唤醒参与者的策略可以分别设置：

```env
ACTIVE_PARTICIPANT_SPEECH_POLICY=barge_in
NEW_PARTICIPANT_WAKE_POLICY=hold
CONVERSATION_IDLE_TIMEOUT_SECONDS=10
LIVE_INPUT_SILENCE_TIMEOUT_MS=10000
NATIVE_WEB_SEARCH_MODE=auto
PREFERRED_SEARCH_SOURCES=op.gg,leagueoflegends.com,wiki.leagueoflegends.com,reddit.com/r/ARAM
VOICE_ACCESS_MODE=implicit
WAKE_WORD_ENGINE=sherpa_onnx
WAKE_WORD_PHRASE=豆包豆包
GROK_X_SEARCH_ENABLED=false
```

- `barge_in`：打断当前回答并立刻提交新的语音。
- `hold`：完整录制本地语音，等当前回答结束后再提交。
- `ignore`：丢弃本次输入。

`LIVE_INPUT_SILENCE_TIMEOUT_MS` 是本地安全超时。后端原生 VAD 可以更早结束一轮，但静默达到 10 秒时，本地会强制提交并退出 `RECORDING`。双方静默 10 秒后，参与者上传门控会关闭，但后端连接保持预热。Gemini 和 Grok 使用各自的原生搜索；`desktop_voice` 使用当前 ChatGPT/Codex Voice 会话可用的搜索能力。V1 不需要 MCP。

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
- [OpenAI](https://openai.com/)（保留旧版兼容适配器）
- [VB-Audio Voicemeeter](https://vb-audio.com/Voicemeeter/banana.htm)
- [discord.py](https://github.com/Rapptz/discord.py)
- [discord-ext-voice-recv](https://github.com/imayhaveborkedit/discord-ext-voice-recv)
- [openWakeWord](https://github.com/dscripka/openWakeWord)
- [webrtcvad-wheels](https://github.com/wiseman/py-webrtcvad-wheels)
- [NumPy](https://numpy.org/)

## 许可证

本项目使用 MIT 许可证，详见 [LICENSE.md](LICENSE.md)。第三方中文关键词模型文件使用 Apache-2.0 许可证。
