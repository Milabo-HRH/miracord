# MIRA.CORD — Multi-user Interface for Realtime AI

[English](README.md) | [简体中文](README.zh-CN.md)

**MIRA** stands for **Multi-user Interface for Realtime AI**. MIRA.CORD is a
Discord bot that enables a shared voice conversation with Google Gemini Live,
xAI Grok Speech-to-Speech, or an already signed-in ChatGPT/Codex desktop Voice
session directly inside a Discord voice channel.

## Features

- Voice interaction with AI using wake-word and push-to-talk.
- Gemini and Grok as V1 first-class providers, with native Google/Web/X search.
- Optional `desktop_voice` backend that uses a personal ChatGPT/Codex Voice
  subscription through an isolated Windows audio bridge instead of an API key.
- One shared AI conversation per guild with per-user local wake gates.
- Configurable `barge_in`, `hold`, or `ignore` behavior for interruptions.
- Active participants can speak again without repeating the wake word.
- Simple, reaction-based controls for interacting with the bot.
- Configurable voice access: automatic for everyone in the connected voice
  channel by default, or explicit reaction-based consent when desired.
- Switch between AI providers on-the-fly with a command.

## Technical Highlights

- **Concurrent Session Management**: Per-guild isolation with dedicated `GuildSession` instances prevents state leaks between Discord servers
- **Thread-Safe Audio Pipeline**: Bridges Discord's synchronous audio thread with asyncio using `run_coroutine_threadsafe` and atomic state capture to prevent race conditions
- **Provider-Agnostic AI Interface**: Runtime switching among Gemini, Grok,
  legacy OpenAI Realtime, and a local desktop Voice bridge
- **Resilient Connections**: Exponential backoff reconnection (1s→30s cap) with graceful shutdown handling
- **Dual Audio Processing**: Strategy pattern with stateful streaming (audioop) and batch quality (pydub) processors
- **Containerized Deployment**: Docker support with Python runtime and FFmpeg bundled in the image

## Getting Started

Follow these steps to set up and run the bot on your local machine or a virtual machine (VM).

### 1. Prerequisites

Before you begin, ensure you have the following software installed:

- **Python 3.11 or newer** (tested on 3.13)
- **FFmpeg**:
  - **Windows**: `winget install -e --id Gyan.FFmpeg`
  - **macOS**: `brew install ffmpeg`
  - **Debian/Ubuntu**: `sudo apt-get install ffmpeg`

The `desktop_voice` provider additionally requires Windows, an installed and
signed-in ChatGPT or Codex desktop app with Voice, and
[Voicemeeter Banana](https://vb-audio.com/Voicemeeter/banana.htm). Voicemeeter
is donationware and its installer requests a Windows restart.

You will also need to gather API keys and set up your Discord bot.

#### Setting Up API Keys and Discord Bot

1.  **Discord Bot Token:**
    - Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a "New Application".
    - Go to the "Installation" tab and set the "Install Link" to "None". This ensures you will use a custom-generated invite URL with the correct permissions.
    - Navigate to the "Bot" tab, in "Token" section, click "Reset Token" to obtain a private token and keep it secured.
    - Within "Bot" tab, disable the "Public Bot" option. This is a crucial security step to prevent others from inviting your bot to their servers.
    - Message Content Intent is not required for the default slash-command setup. Enable it only if you set `ENABLE_PREFIX_COMMANDS=true` for legacy text commands.
    - **Invite the bot to your server:** 
      - Go to the "OAuth2" tab and go to the "OAuth2 URL Generator" section.
      - Select both `bot` and `applications.commands` as scopes and ensure `Guild Install` is the selected integration type.
      - In the "Bot Permissions" section, grant the following permissions:
        - `View Channels`
        - `Send Messages`
        - `Add Reactions`
        - `Connect`
        - `Speak`
      - Make sure you have sufficient permission to invite bot to the server you want to use the bot within.
      - Copy the generated URL and paste it into your browser to add the bot to your server.

2.  **Google Gemini API Key:**
    - Obtain your key from [Google AI Studio](https://aistudio.google.com/app/apikey).

3.  **xAI API Key:**
    - Obtain your key from the [xAI Console](https://console.x.ai/).

### 2. Installation and Configuration

1.  **Clone the Repository**
    ```bash
    git clone https://github.com/Milabo-HRH/miracord.git
    cd miracord
    ```

2.  **Set Up a Virtual Environment**
    Create and activate a virtual environment in the project directory.

    **On Windows:**
    ```bash
    # Create the environment
    py -m venv .venv
    # Activate it
    .\.venv\Scripts\activate
    ```

    **On macOS/Linux:**
    ```bash
    # Create the environment
    python -m venv .venv
    # Activate it
    source .venv/bin/activate
    ```
    *You should see `(.venv)` at the beginning of your command prompt.*

3.  **Install Dependencies**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure API Keys**
    - For a no-IDE setup, run `py scripts/configure.py`. It prompts for the
      Discord token and one provider key without echoing either value, then
      writes the Git-ignored `.env` file for you.
    - Create a file named `.env` in the project's root directory.
    - Add your credentials to it like this:
      ```
      DISCORD_TOKEN=your_discord_token
      GEMINI_API_KEY=your_gemini_api_key
      XAI_API_KEY=your_xai_api_key
      AI_SERVICE_PROVIDER=gemini
      ```
    *Note: Set the key for the provider selected by `AI_SERVICE_PROVIDER`.
    `desktop_voice` uses the signed-in desktop app and does not require an
    OpenAI API key.*

### 3. Running the Bot

This project can be run locally or on a server/VM. With your virtual environment still activated, start the bot.

**On Windows:**
```bash
py main.py
```

**On macOS/Linux:**
```bash
python main.py
```

## Usage

### Bot Commands

| Command | Description | Example |
|---|---|---|
| `/connect` | Joins your voice channel and enters standby mode. | `/connect` |
| `/disconnect` | Leaves the voice channel and resets the bot. | `/disconnect` |
| `/set` | Sets the provider (`gemini`, `grok`, or `desktop_voice`; `openai` is legacy-compatible). | `/set desktop_voice` |

These are registered Discord slash commands. Global command registration can take a few minutes to appear after the bot starts.

### ChatGPT/Codex desktop Voice bridge

This is an OS-audio bridge, not a hidden subscription API:

```text
Discord -> Voicemeeter Input -> B1 -> desktop Voice microphone
desktop Voice speaker -> Voicemeeter AUX Input -> B2 -> Discord
```

One-time Windows setup:

1. Install Voicemeeter Banana and restart Windows when practical.
2. In Windows **Volume mixer**, route only ChatGPT/Codex input to
   `Voicemeeter Out B1` and its output to `Voicemeeter AUX Input`.
3. In Discord **Voice & Video**, explicitly select the real microphone and
   headphones/speakers. Do not leave Discord on a Voicemeeter B1/B2 device.
4. Start one Voice conversation in ChatGPT or Codex.
5. Use `/connect`, then `/set desktop_voice` in Discord.

Do not make B1/B2 the global Windows recording device on a computer that is
also running your personal Discord client. Doing so replaces Discord's real
microphone and can create a playback loop. On a dedicated bot computer with no
personal Discord client, using B1/AUX as the system defaults is acceptable.

The provider starts Voicemeeter Banana, routes virtual strip 3 only to B1 and
virtual strip 4 only to B2, and captures only B2. It locally detects the start
and end of the desktop model's speech before forwarding PCM to Discord. This
prevents the model's answer from being fed back into its own microphone.

The bundled Mandarin keyword-spotting files are the minimal INT8 runtime subset
of `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`. They are distributed
under Apache-2.0; see the model directory's README and license file for source
and attribution details.

The current Windows unified Voicemeeter driver is configured with:

```env
DESKTOP_VOICE_SEND_DEVICE=Voicemeeter Input
DESKTOP_VOICE_RECEIVE_DEVICE=Voicemeeter Out B2
DESKTOP_VOICE_HOST_API=MME
DESKTOP_VOICE_AUTO_ROUTE=true
```

Only one desktop Voice conversation can be bridged at a time, which matches
the V1 `guild_serial` shared-session design. The desktop app must remain open;
if Voice ends or requests user attention, start/reconnect it in the app.

### Voice Controls

After using `/connect`, the bot posts a status message. Use reactions on that message to control it. There are two ways to start a recording:

1.  **Push-to-Talk:**
    - **Add 🎙️ Reaction:** Start recording your voice.
    - **Remove 🎙️ Reaction:** Stop recording and send your audio to the AI.

2.  **Wake Word:**
    - Everyone in the connected voice channel is enabled automatically by default; no 👂 reaction is required.
    - Say the wake word ("豆包豆包") to start recording.
    - The bot will automatically detect when you stop speaking and send your audio to the AI.
    - After joining the shared conversation, that user can continue speaking without repeating the wake word.
    - A different user must say the wake word once before their audio upload gate opens.
    - The default `sherpa_onnx` detector runs locally and uses the configurable Mandarin phrase `WAKE_WORD_PHRASE=豆包豆包`.

Set `VOICE_ACCESS_MODE=explicit` to restore the optional 👂 reaction-based consent flow.

### Shared conversation and interruption settings

The default `guild_serial` mode keeps one provider session and one output stream per guild. Both existing participants and newly awakened participants default to `barge_in`; change either policy independently:

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

`hold` records the full local utterance and submits it after the current answer; `ignore` drops it. `LIVE_INPUT_SILENCE_TIMEOUT_MS` is a local safety net: provider-native VAD may finish a turn earlier, but a silent realtime turn is force-submitted after 10 seconds instead of remaining stuck in `RECORDING`. After 10 seconds of bilateral silence, participant upload gates close while the provider connection stays warm. Gemini and Grok use native search directly; `desktop_voice` uses whatever search capability is available in the active ChatGPT/Codex Voice conversation. V1 does not require MCP.

## Running with Docker

If you prefer containerized deployment for API providers:

1. **Build the image:**
   ```bash
   docker build -t miracord .
   ```

2. **Run the container:**
   ```bash
   docker run --env-file .env miracord
   ```

   Or with individual environment variables:
   ```bash
   docker run -e DISCORD_TOKEN=xxx -e GEMINI_API_KEY=xxx miracord
   ```

`desktop_voice` is intentionally not supported in Docker because it requires
the interactive Windows audio session and desktop Voice UI.

## Troubleshooting

If you encounter issues, please verify that:
- Your virtual environment is active (you see `(.venv)` in your terminal).
- All dependencies from `requirements.txt` are installed.
- The `.env` file exists and contains valid API keys.
- The bot was invited to your server with the correct permissions (see Step 1).
- You are in the project's root directory when running the bot.

## Acknowledgments

- [Google Gemini](https://ai.google.dev/)
- [xAI](https://x.ai/)
- [OpenAI](https://openai.com/) (legacy-compatible adapter)
- [VB-Audio Voicemeeter](https://vb-audio.com/Voicemeeter/banana.htm)
- [discord.py](https://github.com/Rapptz/discord.py)
- [discord-ext-voice-recv](https://github.com/imayhaveborkedit/discord-ext-voice-recv)
- [openWakeWord](https://github.com/dscripka/openWakeWord)
- [webrtcvad-wheels](https://github.com/wiseman/py-webrtcvad-wheels)
- [NumPy](https://numpy.org/)

## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details.
